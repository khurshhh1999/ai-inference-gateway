import hashlib
import time
import uuid

from app.models import (
    ChatChoice,
    ChatChoiceMessage,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Usage,
)
from app.providers.base import Provider


class MockProvider(Provider):
    """Deterministic local provider — no cloud credentials required."""

    name = "mock"

    def __init__(self, latency_ms: int = 40) -> None:
        self._latency_ms = latency_ms

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        if self._latency_ms > 0:
            # Keep Step 1 sync-friendly; tiny sleep simulates network RTT.
            import asyncio

            await asyncio.sleep(self._latency_ms / 1000)

        last_user = next(
            (m.content for m in reversed(request.messages) if m.role == "user"),
            "",
        )
        digest = hashlib.sha256(last_user.encode("utf-8")).hexdigest()[:8]
        content = (
            f"[mock:{request.model}] Echo ({digest}): "
            f"{last_user[:240] or '(empty prompt)'}"
        )
        prompt_tokens = max(1, sum(len(m.content.split()) for m in request.messages))
        completion_tokens = max(1, len(content.split()))

        return ChatCompletionResponse(
            id=f"chatcmpl-mock-{uuid.uuid4().hex[:12]}",
            created=int(time.time()),
            model=request.model,
            choices=[
                ChatChoice(message=ChatChoiceMessage(content=content)),
            ],
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
            provider=self.name,
            cached=False,
        )
