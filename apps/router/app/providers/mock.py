from __future__ import annotations

import hashlib
import json
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
    FunctionCallBody,
    ToolCall,
    ToolChoiceNamed,
    ToolSpec,
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

        if request.messages[-1].role == "tool":
            return self._final_from_tool_results(request)

        selected = _select_mock_tool(request)
        if selected is not None:
            return self._tool_call_response(request, selected)

        last_user = next(
            (m.content or "" for m in reversed(request.messages) if m.role == "user"),
            "",
        )
        digest = hashlib.sha256(last_user.encode("utf-8")).hexdigest()[:8]
        content = (
            f"[{self.name}:{request.model}] Echo ({digest}): "
            f"{last_user[:240] or '(empty prompt)'}"
        )
        return self._text_response(request, content)

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[str]:
        import asyncio

        # Failures / latency match complete() so routing failover behaves the same.
        result = await self.complete(request)
        message = result.choices[0].message
        if message.tool_calls:
            # HTTP streaming synthesizes SSE from complete(); keep a first byte
            # so open_stream() failover still observes a non-empty iterator.
            yield ""
            return
        text = message.content or ""
        words = text.split(" ")
        for i, word in enumerate(words):
            # Tiny pause so clients observe incremental tokens (and abort mid-stream).
            if self._latency_ms == 0:
                await asyncio.sleep(0)
            else:
                await asyncio.sleep(min(0.02, self._latency_ms / 1000.0 / max(1, len(words))))
            yield word if i == len(words) - 1 else word + " "

    def estimate_cost(self, request: ChatCompletionRequest) -> CostEstimate:
        prompt_tokens = request.prompt_token_estimate()
        # Rough completion guess used only for prefer_cost ordering.
        completion_tokens = request.max_tokens or 64
        in_rate = settings.cost_per_1k_input.get(self.name, 0.0)
        out_rate = settings.cost_per_1k_output.get(self.name, 0.0)
        return CostEstimate(
            input_cost_usd=(prompt_tokens / 1000.0) * in_rate,
            output_cost_usd=(completion_tokens / 1000.0) * out_rate,
        )

    def _text_response(self, request: ChatCompletionRequest, content: str) -> ChatCompletionResponse:
        prompt_tokens = request.prompt_token_estimate()
        completion_tokens = max(1, len(content.split()))
        return ChatCompletionResponse(
            id=f"chatcmpl-{self.name}-{uuid.uuid4().hex[:12]}",
            created=int(time.time()),
            model=request.model,
            choices=[ChatChoice(message=ChatChoiceMessage(content=content))],
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
            provider=self.name,
            cached=False,
        )

    def _tool_call_response(
        self,
        request: ChatCompletionRequest,
        tool: ToolSpec,
    ) -> ChatCompletionResponse:
        last_user = next(
            (m.content or "" for m in reversed(request.messages) if m.role == "user"),
            "",
        )
        arguments = _mock_tool_arguments(tool, last_user)
        call = ToolCall(
            id=f"call_{uuid.uuid4().hex[:10]}",
            function=FunctionCallBody(name=tool.function.name, arguments=arguments),
        )
        prompt_tokens = request.prompt_token_estimate()
        completion_tokens = max(1, len(tool.function.name.split()) + len(arguments.split()))
        return ChatCompletionResponse(
            id=f"chatcmpl-{self.name}-{uuid.uuid4().hex[:12]}",
            created=int(time.time()),
            model=request.model,
            choices=[
                ChatChoice(
                    message=ChatChoiceMessage(content=None, tool_calls=[call]),
                    finish_reason="tool_calls",
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

    def _final_from_tool_results(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        results = [m for m in request.messages if m.role == "tool"]
        last = results[-1] if results else request.messages[-1]
        preview = (last.content or "")[:240]
        name = last.name or last.tool_call_id or "tool"
        content = f"[{self.name}:{request.model}] Using {name} result: {preview or '(empty)'}"
        return self._text_response(request, content)


def _select_mock_tool(request: ChatCompletionRequest) -> ToolSpec | None:
    if not request.tools:
        return None
    choice = request.tool_choice
    if choice == "none":
        return None
    if isinstance(choice, ToolChoiceNamed):
        wanted = choice.function.name
        for tool in request.tools:
            if tool.function.name == wanted:
                return tool
        return None
    if choice == "required":
        return request.tools[0]
    last_user = next(
        (m.content or "" for m in reversed(request.messages) if m.role == "user"),
        "",
    ).lower()
    for tool in request.tools:
        if tool.function.name.lower() in last_user:
            return tool
    triggers = ("use tool", "call tool", "invoke ", "use function", "function call")
    if any(trigger in last_user for trigger in triggers):
        return request.tools[0]
    return None


def _mock_tool_arguments(tool: ToolSpec, last_user: str) -> str:
    remainder = last_user
    name = tool.function.name
    idx = last_user.lower().find(name.lower())
    if idx >= 0:
        remainder = last_user[idx + len(name) :].strip(" \t:,-")
    params = (tool.function.parameters or {}).get("properties") or {}
    required = (tool.function.parameters or {}).get("required") or []
    value = remainder or last_user
    if required and isinstance(required, list) and len(required) == 1:
        return json.dumps({str(required[0]): value})
    if isinstance(params, dict) and len(params) == 1:
        key = next(iter(params))
        return json.dumps({str(key): value})
    return json.dumps({"input": value})
