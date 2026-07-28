from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.api.chat import router as chat_router
from app.config import settings
from app.providers import get_provider

app = FastAPI(
    title="AI Inference Router",
    version="0.1.0",
    description="Routing engine for the AI Inference Gateway",
)

app.include_router(chat_router)


@app.get("/health")
async def health() -> JSONResponse:
    provider = get_provider()
    ok = await provider.health()
    return JSONResponse(
        {
            "status": "ok" if ok else "degraded",
            "service": "router",
            "provider_mode": settings.provider_mode,
            "provider": provider.name,
        }
    )
