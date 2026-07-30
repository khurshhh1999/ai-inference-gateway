from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass

from app.models import ChatCompletionRequest, ChatCompletionResponse


@dataclass(frozen=True)
class CostEstimate:
    input_cost_usd: float
    output_cost_usd: float

    @property
    def total_usd(self) -> float:
        return self.input_cost_usd + self.output_cost_usd


class Provider(ABC):
    """Pluggable LLM backend (mock / Bedrock / Vertex)."""

    name: str

    @abstractmethod
    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        raise NotImplementedError

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[str]:
        """Yield incremental text deltas (router wraps these as SSE)."""
        raise NotImplementedError(f"Streaming is not implemented for provider '{self.name}'")
        yield ""  # pragma: no cover

    @abstractmethod
    def estimate_cost(self, request: ChatCompletionRequest) -> CostEstimate:
        raise NotImplementedError

    async def health(self) -> bool:
        return True
