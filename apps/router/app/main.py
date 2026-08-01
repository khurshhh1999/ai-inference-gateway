from __future__ import annotations

import logging

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

from app.api.chat import router as chat_router
from app.budget.meter import get_budget_meter
from app.cache.semantic import get_semantic_cache
from app.config import settings
from app.metrics import render_latest
from app.providers import get_routing_engine

logging.basicConfig(level=settings.log_level.upper())

app = FastAPI(
    title="AI Inference Router",
    version="0.6.0",
    description="Routing engine for the AI Inference Gateway",
)

app.include_router(chat_router)


@app.get("/health")
async def health() -> JSONResponse:
    engine = get_routing_engine()
    provider_health = await engine.health()
    cache_ok = True
    if settings.cache_enabled:
        try:
            cache_ok = await get_semantic_cache().ping()
        except Exception:  # noqa: BLE001
            cache_ok = False
    budget_ok = True
    if settings.budget_enabled:
        try:
            budget_ok = await get_budget_meter().ping()
        except Exception:  # noqa: BLE001
            budget_ok = False
    ok = (any(provider_health.values()) if provider_health else False) and (
        cache_ok or not settings.cache_enabled
    ) and (budget_ok or not settings.budget_enabled)
    return JSONResponse(
        {
            "status": "ok" if ok else "degraded",
            "service": "router",
            "provider_mode": settings.provider_mode,
            "routing_policy": settings.routing_policy,
            "providers": provider_health,
            "cache": {
                "enabled": settings.cache_enabled,
                "healthy": cache_ok,
                "similarity_threshold": settings.cache_similarity_threshold,
            },
            "budget": {
                "enabled": settings.budget_enabled,
                "healthy": budget_ok,
                "soft_ratio": settings.budget_soft_ratio,
                "hard_status": settings.budget_hard_status,
            },
        }
    )


@app.get("/metrics")
async def metrics() -> Response:
    body, content_type = render_latest()
    return Response(content=body, media_type=content_type)
