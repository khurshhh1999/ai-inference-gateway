from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from app.errors import AllProvidersFailedError
from app.models import ChatCompletionRequest, ChatCompletionResponse
from app.providers import get_routing_engine

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(body: ChatCompletionRequest) -> ChatCompletionResponse:
    if body.stream:
        # Step 4 will implement SSE; reject early so clients fail loudly.
        raise HTTPException(
            status_code=501,
            detail="Streaming is not implemented yet (Step 4).",
        )

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

    return decision.response
