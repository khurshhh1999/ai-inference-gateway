from __future__ import annotations

import logging

from fastapi import APIRouter, Header, HTTPException

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

logger = logging.getLogger(__name__)
router = APIRouter()


def _truthy_bypass(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


@router.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(
    body: ChatCompletionRequest,
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_cache_bypass: str | None = Header(default=None, alias="X-Cache-Bypass"),
) -> ChatCompletionResponse:
    if body.stream:
        # Step 4 will implement SSE; reject early so clients fail loudly.
        raise HTTPException(
            status_code=501,
            detail="Streaming is not implemented yet (Step 4).",
        )

    tenant = (x_tenant_id or "default").strip() or "default"
    bypass = _truthy_bypass(x_cache_bypass)
    cache = get_semantic_cache()
    prompt = prompt_from_messages(body.messages)

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
        raise HTTPException(
            status_code=502,
            detail={
                "error": "all_providers_failed",
                "message": str(exc),
                "attempts": exc.attempts,
            },
        ) from exc

    response = decision.response
    cost_usd = estimate_response_cost_usd(
        provider=decision.provider,
        prompt_tokens=response.usage.prompt_tokens,
        completion_tokens=response.usage.completion_tokens,
        settings=settings,
    )
    if cost_usd <= 0.0 and decision.provider == "mock":
        cost_usd = settings.cache_mock_savings_usd

    if not bypass:
        try:
            await cache.store(
                tenant=tenant,
                model=body.model,
                prompt=prompt,
                response=response,
                cost_usd=cost_usd,
            )
        except Exception:  # noqa: BLE001
            logger.exception("cache store failed tenant=%s model=%s", tenant, body.model)

    return response


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
