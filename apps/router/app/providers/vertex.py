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


class VertexGenerativeModel(Protocol):
    def generate_content(self, contents: Any, **kwargs: Any) -> Any: ...

    # Streaming uses the same method with stream=True (returns an iterator).


class VertexModelFactory(Protocol):
    def __call__(self, model_name: str) -> VertexGenerativeModel: ...


class VertexProvider(Provider):
    """GCP Vertex AI adapter (Gemini via generative models).

    Inject ``model_factory`` in tests to avoid the Vertex SDK / credentials.
    """

    name = "vertex"

    def __init__(
        self,
        *,
        model_factory: VertexModelFactory | None = None,
        project: str | None = None,
        location: str | None = None,
    ) -> None:
        self._model_factory = model_factory
        self._project = project if project is not None else settings.google_cloud_project
        self._location = location or settings.google_cloud_location
        self._initialized = model_factory is not None

    def _ensure_sdk(self) -> VertexModelFactory:
        if self._model_factory is not None:
            return self._model_factory
        try:
            import vertexai  # type: ignore[import-untyped]
            from vertexai.generative_models import GenerativeModel  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ProviderError(
                "google-cloud-aiplatform is required for Vertex; "
                "pip install 'ai-inference-router[vertex]'",
                provider=self.name,
                retryable=False,
            ) from exc

        if not self._project:
            raise ProviderError(
                "GOOGLE_CLOUD_PROJECT is required for Vertex",
                provider=self.name,
                retryable=False,
            )
        if not self._initialized:
            vertexai.init(project=self._project, location=self._location)
            self._initialized = True

        def factory(model_name: str) -> VertexGenerativeModel:
            return GenerativeModel(model_name)

        self._model_factory = factory
        return self._model_factory

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        import asyncio

        physical = settings.resolve_physical_model(request.model, self.name)
        prompt = self._format_prompt(request)

        def _generate() -> Any:
            factory = self._ensure_sdk()
            model = factory(physical)
            kwargs: dict[str, Any] = {}
            generation: dict[str, Any] = {}
            if request.max_tokens is not None:
                generation["max_output_tokens"] = request.max_tokens
            if request.temperature is not None:
                generation["temperature"] = request.temperature
            if generation:
                kwargs["generation_config"] = generation
            kwargs.update(_vertex_tool_kwargs(request))
            return model.generate_content(prompt, **kwargs)

        try:
            result = await asyncio.to_thread(_generate)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(str(exc), provider=self.name, retryable=True) from exc

        return self._parse_response(result, request.model, physical)

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[str]:
        import asyncio

        from app.streaming import iterate_sync_iterator

        physical = settings.resolve_physical_model(request.model, self.name)
        prompt = self._format_prompt(request)

        def _generate() -> Any:
            factory = self._ensure_sdk()
            model = factory(physical)
            kwargs: dict[str, Any] = {"stream": True}
            generation: dict[str, Any] = {}
            if request.max_tokens is not None:
                generation["max_output_tokens"] = request.max_tokens
            if request.temperature is not None:
                generation["temperature"] = request.temperature
            if generation:
                kwargs["generation_config"] = generation
            kwargs.update(_vertex_tool_kwargs(request))
            return model.generate_content(prompt, **kwargs)

        try:
            result = await asyncio.to_thread(_generate)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(str(exc), provider=self.name, retryable=True) from exc

        async for chunk in iterate_sync_iterator(result):
            text = ""
            if hasattr(chunk, "text"):
                try:
                    text = chunk.text or ""
                except Exception:  # noqa: BLE001
                    text = ""
            elif isinstance(chunk, str):
                text = chunk
            if text:
                yield text

    def estimate_cost(self, request: ChatCompletionRequest) -> CostEstimate:
        prompt_tokens = request.prompt_token_estimate()
        completion_tokens = request.max_tokens or 64
        in_rate = settings.cost_per_1k_input.get(self.name, 0.000075)
        out_rate = settings.cost_per_1k_output.get(self.name, 0.0003)
        return CostEstimate(
            input_cost_usd=(prompt_tokens / 1000.0) * in_rate,
            output_cost_usd=(completion_tokens / 1000.0) * out_rate,
        )

    async def health(self) -> bool:
        try:
            if self._model_factory is not None:
                return True
            if not self._project:
                return False
            self._ensure_sdk()
            return True
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _format_prompt(request: ChatCompletionRequest) -> str:
        parts: list[str] = []
        for message in request.messages:
            text = message.content or ""
            if message.tool_calls:
                dumped = [
                    {
                        "id": call.id,
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    }
                    for call in message.tool_calls
                ]
                text = f"{text} tool_calls={dumped}".strip()
            if message.role == "tool":
                text = f"[tool_result {message.tool_call_id}] {text}"
            parts.append(f"{message.role}: {text}")
        return "\n".join(parts)

    def _parse_response(
        self,
        result: Any,
        logical_model: str,
        physical: str,
    ) -> ChatCompletionResponse:
        content = ""
        if hasattr(result, "text"):
            try:
                content = result.text or ""
            except Exception:  # noqa: BLE001 — some SDK responses lack .text
                content = str(result)
        else:
            content = str(result)

        tool_calls = _vertex_tool_calls(result)
        if tool_calls and not content:
            content = None

        usage_meta = getattr(result, "usage_metadata", None)
        prompt_tokens = int(getattr(usage_meta, "prompt_token_count", None) or 1)
        completion_tokens = int(
            getattr(usage_meta, "candidates_token_count", None)
            or max(1, len((content or "").split()))
        )

        logger.debug("vertex complete model=%s physical=%s", logical_model, physical)
        return ChatCompletionResponse(
            id=f"chatcmpl-vertex-{uuid.uuid4().hex[:12]}",
            created=int(time.time()),
            model=logical_model,
            choices=[
                ChatChoice(
                    message=ChatChoiceMessage(
                        content=content,
                        tool_calls=tool_calls or None,
                    ),
                    finish_reason="tool_calls" if tool_calls else "stop",
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


def _vertex_tool_kwargs(request: ChatCompletionRequest) -> dict[str, Any]:
    if not request.tools:
        return {}
    declarations = [
        {
            "name": tool.function.name,
            "description": tool.function.description or "",
            "parameters": tool.function.parameters or {"type": "object", "properties": {}},
        }
        for tool in request.tools
    ]
    tools: Any
    try:
        from vertexai.generative_models import (  # type: ignore[import-untyped]
            FunctionDeclaration,
            Tool,
        )

        tools = [
            Tool(
                function_declarations=[
                    FunctionDeclaration(
                        name=item["name"],
                        description=item["description"],
                        parameters=item["parameters"],
                    )
                    for item in declarations
                ]
            )
        ]
    except ImportError:
        tools = [{"function_declarations": declarations}]
    kwargs: dict[str, Any] = {"tools": tools}
    mode = "AUTO"
    if request.tool_choice == "none":
        mode = "NONE"
    elif request.tool_choice == "required":
        mode = "ANY"
    elif isinstance(request.tool_choice, ToolChoiceNamed):
        kwargs["tool_config"] = {
            "function_calling_config": {
                "mode": "ANY",
                "allowed_function_names": [request.tool_choice.function.name],
            }
        }
        return kwargs
    kwargs["tool_config"] = {"function_calling_config": {"mode": mode}}
    return kwargs


def _vertex_tool_calls(result: Any) -> list[ToolCall]:
    found: list[Any] = []
    direct = getattr(result, "function_calls", None)
    if direct:
        found.extend(list(direct))
    candidates = getattr(result, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            call = getattr(part, "function_call", None)
            if call is not None:
                found.append(call)
    calls: list[ToolCall] = []
    for raw in found:
        if isinstance(raw, dict):
            name = str(raw.get("name") or "unknown")
            args: Any = raw.get("args") or raw.get("arguments") or {}
        else:
            name = str(getattr(raw, "name", None) or "unknown")
            args = getattr(raw, "args", None)
        if args is None:
            args = {}
        if hasattr(args, "items") and not isinstance(args, dict):
            try:
                args = dict(args)
            except Exception:  # noqa: BLE001
                args = {"raw": str(args)}
        calls.append(
            ToolCall(
                id=f"call_{uuid.uuid4().hex[:10]}",
                function=FunctionCallBody(
                    name=name or "unknown",
                    arguments=json.dumps(args) if not isinstance(args, str) else args,
                ),
            )
        )
    return calls
