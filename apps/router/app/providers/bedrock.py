from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, Protocol

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

logger = logging.getLogger(__name__)


class BedrockClient(Protocol):
    def invoke_model(self, **kwargs: Any) -> dict[str, Any]: ...

    def invoke_model_with_response_stream(self, **kwargs: Any) -> dict[str, Any]: ...


class BedrockProvider(Provider):
    """AWS Bedrock adapter (Claude Messages API via invoke_model).

    Pass an injected ``client`` in tests; otherwise boto3 is imported lazily so
    local/CI mock runs never need AWS credentials.
    """

    name = "bedrock"

    def __init__(
        self,
        *,
        client: BedrockClient | None = None,
        region: str | None = None,
    ) -> None:
        self._client = client
        self._region = region or settings.aws_region

    def _get_client(self) -> BedrockClient:
        if self._client is not None:
            return self._client
        try:
            import boto3  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ProviderError(
                "boto3 is required for Bedrock; pip install 'ai-inference-router[bedrock]'",
                provider=self.name,
                retryable=False,
            ) from exc
        self._client = boto3.client("bedrock-runtime", region_name=self._region)
        return self._client

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        import asyncio

        physical = settings.resolve_physical_model(request.model, self.name)
        body = self._build_body(request, physical)

        def _invoke() -> dict[str, Any]:
            client = self._get_client()
            return client.invoke_model(
                modelId=physical,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(body),
            )

        try:
            raw = await asyncio.to_thread(_invoke)
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 — map SDK errors uniformly
            raise ProviderError(str(exc), provider=self.name, retryable=True) from exc

        return self._parse_response(raw, request.model, physical)

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[str]:
        import asyncio

        from app.streaming import iterate_sync_iterator

        physical = settings.resolve_physical_model(request.model, self.name)
        body = self._build_body(request, physical)

        def _invoke() -> dict[str, Any]:
            client = self._get_client()
            return client.invoke_model_with_response_stream(
                modelId=physical,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(body),
            )

        try:
            raw = await asyncio.to_thread(_invoke)
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(str(exc), provider=self.name, retryable=True) from exc

        event_stream = raw.get("body")
        if event_stream is None:
            raise ProviderError(
                "Bedrock stream response missing body",
                provider=self.name,
                retryable=True,
            )

        async for event in iterate_sync_iterator(event_stream):
            delta = self._delta_from_stream_event(event)
            if delta:
                yield delta

    def estimate_cost(self, request: ChatCompletionRequest) -> CostEstimate:
        prompt_tokens = max(1, sum(len(m.content.split()) for m in request.messages))
        completion_tokens = request.max_tokens or 64
        in_rate = settings.cost_per_1k_input.get(self.name, 0.00025)
        out_rate = settings.cost_per_1k_output.get(self.name, 0.00125)
        return CostEstimate(
            input_cost_usd=(prompt_tokens / 1000.0) * in_rate,
            output_cost_usd=(completion_tokens / 1000.0) * out_rate,
        )

    async def health(self) -> bool:
        try:
            self._get_client()
            return True
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _build_body(request: ChatCompletionRequest, _physical: str) -> dict[str, Any]:
        # Anthropic Messages-style payload used by Claude on Bedrock.
        system_parts = [m.content for m in request.messages if m.role == "system"]
        messages = [
            {"role": m.role, "content": m.content}
            for m in request.messages
            if m.role in {"user", "assistant"}
        ]
        body: dict[str, Any] = {
            "anthropic_version": "bedrock-2023-05-31",
            "messages": messages or [{"role": "user", "content": ""}],
            "max_tokens": request.max_tokens or 512,
        }
        if system_parts:
            body["system"] = "\n".join(system_parts)
        if request.temperature is not None:
            body["temperature"] = request.temperature
        return body

    @staticmethod
    def _delta_from_stream_event(event: Any) -> str:
        """Extract text from a Bedrock/Claude response-stream event."""
        if not isinstance(event, dict):
            return ""

        chunk = event.get("chunk", event)
        data: Any = None
        if isinstance(chunk, dict):
            payload = chunk.get("bytes")
            if isinstance(payload, (bytes, bytearray)):
                data = json.loads(payload.decode("utf-8"))
            elif isinstance(payload, str):
                data = json.loads(payload)
            elif isinstance(payload, dict):
                data = payload
            elif "type" in chunk or "delta" in chunk or "completion" in chunk:
                data = chunk
        if not isinstance(data, dict):
            return ""

        # Anthropic Messages stream on Bedrock.
        if data.get("type") == "content_block_delta":
            delta = data.get("delta") or {}
            if isinstance(delta, dict):
                return str(delta.get("text") or "")
        delta = data.get("delta")
        if isinstance(delta, dict) and delta.get("text"):
            return str(delta["text"])
        if isinstance(data.get("completion"), str):
            return data["completion"]
        if isinstance(data.get("outputText"), str):
            return data["outputText"]
        return ""

    def _parse_response(
        self,
        raw: dict[str, Any],
        logical_model: str,
        physical: str,
    ) -> ChatCompletionResponse:
        payload = raw.get("body")
        if hasattr(payload, "read"):
            payload = payload.read()
        if isinstance(payload, (bytes, bytearray)):
            data = json.loads(payload.decode("utf-8"))
        elif isinstance(payload, str):
            data = json.loads(payload)
        elif isinstance(payload, dict):
            data = payload
        else:
            data = raw

        content_blocks = data.get("content") or []
        text_parts = [
            block.get("text", "")
            for block in content_blocks
            if isinstance(block, dict) and block.get("type", "text") == "text"
        ]
        content = "".join(text_parts) or data.get("completion") or data.get("outputText") or ""
        usage_raw = data.get("usage") or {}
        prompt_tokens = int(usage_raw.get("input_tokens") or usage_raw.get("prompt_tokens") or 1)
        completion_tokens = int(
            usage_raw.get("output_tokens") or usage_raw.get("completion_tokens") or max(1, len(content.split()))
        )

        logger.debug("bedrock complete model=%s physical=%s", logical_model, physical)
        return ChatCompletionResponse(
            id=f"chatcmpl-bedrock-{uuid.uuid4().hex[:12]}",
            created=int(time.time()),
            model=logical_model,
            choices=[ChatChoice(message=ChatChoiceMessage(content=content))],
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
            provider=self.name,
            cached=False,
        )
