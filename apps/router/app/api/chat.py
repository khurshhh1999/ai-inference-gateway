from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.cache.metrics import cache_metrics
from app.cache.semantic import (
    estimate_response_cost_usd,
    get_semantic_cache,
    prompt_from_messages,
)
from app.config import settings
from app.errors import AllProvidersFailedError
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


@router.post("/v1/chat/completions", response_model=None)
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_cache_bypass: str | None = Header(default=None, alias="X-Cache-Bypass"),
) -> ChatCompletionResponse | StreamingResponse:
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
        )

    if not bypass:
        try:
            hit = await cache.lookup(tenant=tenant, model=body.model, prompt=prompt)
        except Exception:  # noqa: BLE001
            logger.exception("cache lookup failed tenant=%s model=%s", tenant, body.model)
            hit = None
        if hit is not None:
            return hit.response

    engine = get_routing_engine()
    try:
        decision = await engine.complete(body)
    except AllProvidersFailedError as exc:
        logger.error("all providers failed attempts=%s", exc.attempts)
        raise _all_providers_failed_http(exc) from exc

    response = decision.response
    await _maybe_store_cache(
        tenant=tenant,
        model=body.model,
        prompt=prompt,
        response=response,
        provider=decision.provider,
        bypass=bypass,
    )
    return response


async def _stream_completions(
    *,
    body: ChatCompletionRequest,
    request: Request,
    tenant: str,
    bypass: bool,
    prompt: str,
) -> StreamingResponse:
    cache = get_semantic_cache()

    if not bypass:
        try:
            hit = await cache.lookup(tenant=tenant, model=body.model, prompt=prompt)
        except Exception:  # noqa: BLE001
            logger.exception(
                "cache lookup failed tenant=%s model=%s stream=1", tenant, body.model
            )
            hit = None
        if hit is not None:
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

    engine = get_routing_engine()
    try:
        route = await engine.open_stream(body)
    except AllProvidersFailedError as exc:
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

        if completed and accumulated and not bypass:
            prompt_tokens = max(1, sum(len(m.content.split()) for m in body.messages))
            response = build_completion_from_stream(
                completion_id=route.completion_id,
                created=route.created,
                model=route.model,
                content=accumulated,
                provider=route.provider,
                prompt_tokens=prompt_tokens,
                route_reason=route.reason,
            )
            await _maybe_store_cache(
                tenant=tenant,
                model=body.model,
                prompt=prompt,
                response=response,
                provider=route.provider,
                bypass=bypass,
            )

    return StreamingResponse(
        _provider_events(),
        media_type="text/event-stream",
        headers=_sse_headers(),
    )


def _sse_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }


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
    except Exception:  # noqa: BLE001
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
