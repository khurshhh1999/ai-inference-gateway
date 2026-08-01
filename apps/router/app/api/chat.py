from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from app.budget.meter import BudgetExceededError, get_budget_meter
from app.budget.pricing import billable_cost_usd
from app.cache.metrics import cache_metrics
from app.cache.semantic import (
    estimate_response_cost_usd,
    get_semantic_cache,
    prompt_from_messages,
)
from app.config import settings
from app.errors import AllProvidersFailedError
from app.metrics import (
    observe_request,
    record_budget_rejection,
    record_spend,
)
from app.models import ChatCompletionRequest, ChatCompletionResponse
from app.providers import get_routing_engine
from app.streaming import (
    build_completion_from_stream,
    iter_sse_from_deltas,
    iter_sse_from_text,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _truthy_bypass(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _all_providers_failed_http(exc: AllProvidersFailedError) -> HTTPException:
    return HTTPException(
        status_code=502,
        detail={
            "error": "all_providers_failed",
            "message": str(exc),
            "attempts": exc.attempts,
        },
    )


def _budget_exceeded_http(exc: BudgetExceededError) -> HTTPException:
    record_budget_rejection(exc.window, exc.metric)
    return HTTPException(
        status_code=exc.status_code,
        detail={
            "error": "budget_exceeded",
            "message": str(exc),
            "tenant": exc.tenant,
            "window": exc.window,
            "metric": exc.metric,
            "used": exc.used,
            "limit": exc.limit,
        },
    )


def _apply_budget_headers(response: Response, *, soft_warning: bool) -> None:
    if soft_warning:
        response.headers["X-Budget-Warning"] = "soft"


@router.post("/v1/chat/completions", response_model=None)
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    response: Response,
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_cache_bypass: str | None = Header(default=None, alias="X-Cache-Bypass"),
) -> ChatCompletionResponse | StreamingResponse:
    started = time.perf_counter()
    tenant = (x_tenant_id or "default").strip() or "default"
    bypass = _truthy_bypass(x_cache_bypass)
    cache = get_semantic_cache()
    prompt = prompt_from_messages(body.messages)

    if body.stream:
        return await _stream_completions(
            body=body,
            request=request,
            tenant=tenant,
            bypass=bypass,
            prompt=prompt,
            started=started,
        )

    cached = False
    status = 200
    try:
        if not bypass:
            try:
                hit = await cache.lookup(tenant=tenant, model=body.model, prompt=prompt)
            except Exception:
                logger.exception(
                    "cache lookup failed tenant=%s model=%s", tenant, body.model
                )
                hit = None
            if hit is not None:
                cached = True
                return hit.response

        meter = get_budget_meter()
        try:
            check = await meter.check(tenant)
        except BudgetExceededError as exc:
            status = exc.status_code
            raise _budget_exceeded_http(exc) from exc
        _apply_budget_headers(response, soft_warning=check.soft_warning)

        engine = get_routing_engine()
        try:
            decision = await engine.complete(body)
        except AllProvidersFailedError as exc:
            status = 502
            logger.error("all providers failed attempts=%s", exc.attempts)
            raise _all_providers_failed_http(exc) from exc

        completion = decision.response
        await _meter_completion(
            tenant=tenant,
            model=body.model,
            response=completion,
            provider=decision.provider,
        )
        await _maybe_store_cache(
            tenant=tenant,
            model=body.model,
            prompt=prompt,
            response=completion,
            provider=decision.provider,
            bypass=bypass,
        )
        return completion
    finally:
        observe_request(
            method="POST",
            route="/v1/chat/completions",
            status=status,
            cached=cached,
            stream=False,
            seconds=time.perf_counter() - started,
        )


async def _stream_completions(
    *,
    body: ChatCompletionRequest,
    request: Request,
    tenant: str,
    bypass: bool,
    prompt: str,
    started: float,
) -> StreamingResponse:
    cache = get_semantic_cache()
    status = 200
    was_cached = False

    try:
        if not bypass:
            try:
                hit = await cache.lookup(tenant=tenant, model=body.model, prompt=prompt)
            except Exception:
                logger.exception(
                    "cache lookup failed tenant=%s model=%s stream=1", tenant, body.model
                )
                hit = None
            if hit is not None:
                was_cached = True
                cached = hit.response

                async def _cached_events() -> AsyncIterator[str]:
                    async for frame in iter_sse_from_text(
                        text=cached.choices[0].message.content,
                        model=cached.model,
                        provider=cached.provider,
                        completion_id=cached.id,
                        created=cached.created,
                        cached=True,
                        route_reason="cache_hit",
                    ):
                        if await request.is_disconnected():
                            logger.info("client disconnected during cached stream")
                            break
                        yield frame

                return StreamingResponse(
                    _cached_events(),
                    media_type="text/event-stream",
                    headers=_sse_headers(),
                )

        meter = get_budget_meter()
        try:
            check = await meter.check(tenant)
        except BudgetExceededError as exc:
            status = exc.status_code
            raise _budget_exceeded_http(exc) from exc

        engine = get_routing_engine()
        try:
            route = await engine.open_stream(body)
        except AllProvidersFailedError as exc:
            status = 502
            logger.error("all providers failed attempts=%s stream=1", exc.attempts)
            raise _all_providers_failed_http(exc) from exc

        async def _provider_events() -> AsyncIterator[str]:
            completed = False
            accumulated = ""
            try:
                async for frame, accumulated in iter_sse_from_deltas(
                    route.deltas,
                    model=route.model,
                    provider=route.provider,
                    completion_id=route.completion_id,
                    created=route.created,
                    cached=False,
                    route_reason=route.reason,
                ):
                    if await request.is_disconnected():
                        logger.info(
                            "client disconnected mid-stream provider=%s",
                            route.provider,
                        )
                        break
                    yield frame
                    if "[DONE]" in frame:
                        completed = True
            finally:
                await route.aclose()

            if completed and accumulated:
                prompt_tokens = max(1, sum(len(m.content.split()) for m in body.messages))
                completion = build_completion_from_stream(
                    completion_id=route.completion_id,
                    created=route.created,
                    model=route.model,
                    content=accumulated,
                    provider=route.provider,
                    prompt_tokens=prompt_tokens,
                    route_reason=route.reason,
                )
                await _meter_completion(
                    tenant=tenant,
                    model=body.model,
                    response=completion,
                    provider=route.provider,
                )
                if not bypass:
                    await _maybe_store_cache(
                        tenant=tenant,
                        model=body.model,
                        prompt=prompt,
                        response=completion,
                        provider=route.provider,
                        bypass=bypass,
                    )

        headers = _sse_headers()
        if check.soft_warning:
            headers["X-Budget-Warning"] = "soft"
        return StreamingResponse(
            _provider_events(),
            media_type="text/event-stream",
            headers=headers,
        )
    finally:
        observe_request(
            method="POST",
            route="/v1/chat/completions",
            status=status,
            cached=was_cached,
            stream=True,
            seconds=time.perf_counter() - started,
        )


def _sse_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }


async def _meter_completion(
    *,
    tenant: str,
    model: str,
    response: ChatCompletionResponse,
    provider: str,
) -> None:
    meter = get_budget_meter()
    if not meter.enabled:
        return
    usd = billable_cost_usd(
        provider=provider,
        model=model,
        prompt_tokens=response.usage.prompt_tokens,
        completion_tokens=response.usage.completion_tokens,
        settings=settings,
    )
    tokens = response.usage.total_tokens
    try:
        await meter.record(
            tenant=tenant,
            usd=usd,
            tokens=tokens,
            provider=provider,
            model=model,
        )
        record_spend(provider, usd)
    except Exception:
        logger.exception("budget record failed tenant=%s model=%s", tenant, model)


async def _maybe_store_cache(
    *,
    tenant: str,
    model: str,
    prompt: str,
    response: ChatCompletionResponse,
    provider: str,
    bypass: bool,
) -> None:
    if bypass:
        return
    cache = get_semantic_cache()
    cost_usd = estimate_response_cost_usd(
        provider=provider,
        prompt_tokens=response.usage.prompt_tokens,
        completion_tokens=response.usage.completion_tokens,
        settings=settings,
    )
    if cost_usd <= 0.0 and provider == "mock":
        cost_usd = settings.cache_mock_savings_usd
    try:
        await cache.store(
            tenant=tenant,
            model=model,
            prompt=prompt,
            response=response,
            cost_usd=cost_usd,
        )
    except Exception:
        logger.exception("cache store failed tenant=%s model=%s", tenant, model)


@router.get("/v1/cache/stats")
async def cache_stats() -> dict:
    """Hit/miss counters and estimated USD saved (process-local)."""
    cache = get_semantic_cache()
    return {
        "enabled": cache.enabled,
        "similarity_threshold": cache.similarity_threshold,
        "ttl_seconds": cache.ttl_seconds,
        "max_entries": cache.max_entries,
        **cache_metrics.as_dict(),
    }


@router.get("/v1/tenants/{tenant_id}/usage")
async def tenant_usage(tenant_id: str) -> dict:
    """Usage summary + remaining budget for minute/day/month windows."""
    tenant = tenant_id.strip() or "default"
    meter = get_budget_meter()
    status = await meter.usage(tenant)
    return status.as_dict()


@router.get("/v1/tenants/{tenant_id}/budget")
async def tenant_budget(tenant_id: str) -> dict:
    """Configured soft/hard limits for a tenant."""
    tenant = tenant_id.strip() or "default"
    meter = get_budget_meter()
    limits = meter.limits_for(tenant)
    return {
        "tenant": tenant,
        "enabled": meter.enabled,
        "soft_ratio": settings.budget_soft_ratio,
        "hard_status": meter.hard_status,
        "limits": limits,
    }
