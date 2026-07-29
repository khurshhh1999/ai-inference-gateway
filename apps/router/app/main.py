from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.api.chat import router as chat_router
from app.config import settings
from app.providers import get_routing_engine

logging.basicConfig(level=settings.log_level.upper())

app = FastAPI(
    title="AI Inference Router",
    version="0.2.0",
    description="Routing engine for the AI Inference Gateway",
)

app.include_router(chat_router)


@app.get("/health")
async def health() -> JSONResponse:
    engine = get_routing_engine()
    provider_health = await engine.health()
    ok = any(provider_health.values()) if provider_health else False
    return JSONResponse(
        {
            "status": "ok" if ok else "degraded",
            "service": "router",
            "provider_mode": settings.provider_mode,
            "routing_policy": settings.routing_policy,
            "providers": provider_health,
        }
    )
