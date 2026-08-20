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
    FunctionCallBody,
    ToolCall,
    ToolChoiceNamed,
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
        except Exception as exc:
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
        except Exception as exc:
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
        prompt_tokens = request.prompt_token_estimate()
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
        system_parts, messages = _anthropic_messages(request)
        body: dict[str, Any] = {
            "anthropic_version": "bedrock-2023-05-31",
            "messages": messages or [{"role": "user", "content": ""}],
            "max_tokens": request.max_tokens or 512,
        }
        if system_parts:
            body["system"] = "\n".join(system_parts)
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.tools:
            body["tools"] = [
                {
                    "name": tool.function.name,
                    "description": tool.function.description or "",
                    "input_schema": tool.function.parameters
                    or {"type": "object", "properties": {}},
                }
                for tool in request.tools
            ]
            body["tool_choice"] = _bedrock_tool_choice(request.tool_choice)
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
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type", "text")
            if block_type == "text":
                text_parts.append(str(block.get("text") or ""))
            elif block_type == "tool_use":
                raw_input = block.get("input") or {}
                tool_calls.append(
                    ToolCall(
                        id=str(block.get("id") or f"call_{uuid.uuid4().hex[:10]}"),
                        function=FunctionCallBody(
                            name=str(block.get("name") or "unknown"),
                            arguments=json.dumps(raw_input),
                        ),
                    )
                )
        content = "".join(text_parts) or data.get("completion") or data.get("outputText") or ""
        if tool_calls and not content:
            content = None
        finish_reason = "tool_calls" if tool_calls else "stop"
        if data.get("stop_reason") == "tool_use":
            finish_reason = "tool_calls"
        usage_raw = data.get("usage") or {}
        prompt_tokens = int(usage_raw.get("input_tokens") or usage_raw.get("prompt_tokens") or 1)
        completion_tokens = int(
            usage_raw.get("output_tokens")
            or usage_raw.get("completion_tokens")
            or max(1, len((content or "").split()) + sum(len(c.function.arguments.split()) for c in tool_calls))
        )

        logger.debug("bedrock complete model=%s physical=%s", logical_model, physical)
        return ChatCompletionResponse(
            id=f"chatcmpl-bedrock-{uuid.uuid4().hex[:12]}",
            created=int(time.time()),
            model=logical_model,
            choices=[
                ChatChoice(
                    message=ChatChoiceMessage(
                        content=content,
                        tool_calls=tool_calls or None,
                    ),
                    finish_reason=finish_reason,
                )
            ],
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
            provider=self.name,
            cached=False,
        )


def _bedrock_tool_choice(choice: str | ToolChoiceNamed | None) -> dict[str, Any]:
    if choice is None or choice == "auto":
        return {"type": "auto"}
    if choice == "none":
        return {"type": "none"}
    if choice == "required":
        return {"type": "any"}
    if isinstance(choice, ToolChoiceNamed):
        return {"type": "tool", "name": choice.function.name}
    return {"type": "auto"}


def _anthropic_messages(request: ChatCompletionRequest) -> tuple[list[str], list[dict[str, Any]]]:
    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []
    for message in request.messages:
        if message.role == "system":
            system_parts.append(message.content or "")
            continue
        if message.role == "user":
            messages.append({"role": "user", "content": message.content or ""})
            continue
        if message.role == "assistant":
            if message.tool_calls:
                blocks: list[dict[str, Any]] = []
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                for call in message.tool_calls:
                    try:
                        parsed = json.loads(call.function.arguments or "{}")
                    except json.JSONDecodeError:
                        parsed = {"raw": call.function.arguments}
                    if not isinstance(parsed, dict):
                        parsed = {"value": parsed}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call.id,
                            "name": call.function.name,
                            "input": parsed,
                        }
                    )
                messages.append({"role": "assistant", "content": blocks})
            else:
                messages.append({"role": "assistant", "content": message.content or ""})
            continue
        if message.role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id,
                "content": message.content or "",
            }
            if (
                messages
                and messages[-1].get("role") == "user"
                and isinstance(messages[-1].get("content"), list)
            ):
                messages[-1]["content"].append(block)
            else:
                messages.append({"role": "user", "content": [block]})
    return system_parts, messages
