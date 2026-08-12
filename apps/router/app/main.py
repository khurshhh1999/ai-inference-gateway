from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.api.chat import router as chat_router
from app.budget.meter import get_budget_meter
from app.cache.semantic import get_semantic_cache
from app.config import settings
from app.metrics import render_latest
from app.providers import get_routing_engine
from app.request_id import resolve_request_id
from app.tracing import (
    attach_context,
    detach_context,
    extract_context,
    get_tracer,
    init_tracing,
    set_span_error,
    set_span_ok,
)

logging.basicConfig(level=settings.log_level.upper())
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    init_tracing(settings)
    yield


app = FastAPI(
    title="AI Inference Router",
    version="0.10.0",
    description="Routing engine for the AI Inference Gateway",
    lifespan=lifespan,
)

app.include_router(chat_router)


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    request_id = resolve_request_id(request.headers.get("x-request-id"))
    request.state.request_id = request_id

    parent_ctx = extract_context(request.headers)
    token = attach_context(parent_ctx)
    tracer = get_tracer("router")
    try:
        with tracer.start_as_current_span(
            f"HTTP {request.method} {request.url.path}",
            attributes={
                "http.request_id": request_id,
                "http.method": request.method,
                "http.route": request.url.path,
            },
        ) as span:
            try:
                response: Response = await call_next(request)
            except Exception as exc:
                set_span_error(span, exc)
                raise
            response.headers["X-Request-Id"] = request_id
            span.set_attribute("http.status_code", response.status_code)
            if response.status_code >= 500:
                span.set_attribute("error", True)
            else:
                set_span_ok(span)
            return response
    finally:
        detach_context(token)


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
            "otel": {
                "enabled": settings.otel_enabled,
                "service_name": settings.otel_service_name,
                "otlp_configured": bool(settings.otel_exporter_otlp_endpoint.strip()),
            },
            "adaptive": {
                "ewma_alpha": settings.adaptive_ewma_alpha,
                "error_penalty_ms": settings.adaptive_error_penalty_ms,
                "stale_after_seconds": settings.adaptive_stale_after_seconds,
                "providers": engine.signals_snapshot(),
            },
        }
    )


@app.get("/metrics")
async def metrics() -> Response:
    body, content_type = render_latest()
    return Response(content=body, media_type=content_type)
