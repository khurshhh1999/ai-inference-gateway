from fastapi import APIRouter, HTTPException

from app.models import ChatCompletionRequest, ChatCompletionResponse
from app.providers import get_provider

router = APIRouter()


@router.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(body: ChatCompletionRequest) -> ChatCompletionResponse:
    if body.stream:
        # Step 4 will implement SSE; reject early so clients fail loudly.
        raise HTTPException(
            status_code=501,
            detail="Streaming is not implemented yet (Step 4).",
        )
    provider = get_provider()
    return await provider.complete(body)
