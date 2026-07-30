from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import AsyncIterator

from app.config import settings
from app.errors import ProviderError
from app.models import (
    ChatChoice,
    ChatChoiceMessage,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Usage,
)
from app.providers.base import CostEstimate, Provider


class MockProvider(Provider):
    """Deterministic local provider — no cloud credentials required."""

    name = "mock"

    def __init__(
        self,
        latency_ms: int | None = None,
        *,
        fail_times: int = 0,
        name: str = "mock",
    ) -> None:
        self._latency_ms = settings.mock_latency_ms if latency_ms is None else latency_ms
        self._fail_times = fail_times
        self._fail_remaining = fail_times
        self.name = name

    def reset_failures(self) -> None:
        self._fail_remaining = self._fail_times

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            raise ProviderError(
                f"Injected failure from mock provider '{self.name}'",
                provider=self.name,
                retryable=True,
            )

        if self._latency_ms > 0:
            import asyncio

            await asyncio.sleep(self._latency_ms / 1000)

        last_user = next(
            (m.content for m in reversed(request.messages) if m.role == "user"),
            "",
        )
        digest = hashlib.sha256(last_user.encode("utf-8")).hexdigest()[:8]
        content = (
            f"[{self.name}:{request.model}] Echo ({digest}): "
            f"{last_user[:240] or '(empty prompt)'}"
        )
        prompt_tokens = max(1, sum(len(m.content.split()) for m in request.messages))
        completion_tokens = max(1, len(content.split()))

        return ChatCompletionResponse(
            id=f"chatcmpl-{self.name}-{uuid.uuid4().hex[:12]}",
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

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[str]:
        import asyncio

        # Failures / latency match complete() so routing failover behaves the same.
        result = await self.complete(request)
        text = result.choices[0].message.content
        words = text.split(" ")
        for i, word in enumerate(words):
            # Tiny pause so clients observe incremental tokens (and abort mid-stream).
            if self._latency_ms == 0:
                await asyncio.sleep(0)
            else:
                await asyncio.sleep(min(0.02, self._latency_ms / 1000.0 / max(1, len(words))))
            yield word if i == len(words) - 1 else word + " "

    def estimate_cost(self, request: ChatCompletionRequest) -> CostEstimate:
        prompt_tokens = max(1, sum(len(m.content.split()) for m in request.messages))
        # Rough completion guess used only for prefer_cost ordering.
        completion_tokens = request.max_tokens or 64
        in_rate = settings.cost_per_1k_input.get(self.name, 0.0)
        out_rate = settings.cost_per_1k_output.get(self.name, 0.0)
        return CostEstimate(
            input_cost_usd=(prompt_tokens / 1000.0) * in_rate,
            output_cost_usd=(completion_tokens / 1000.0) * out_rate,
        )
