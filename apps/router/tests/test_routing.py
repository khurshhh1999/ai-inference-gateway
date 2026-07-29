from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import Settings
from app.errors import AllProvidersFailedError, ProviderError
from app.models import ChatCompletionRequest, ChatMessage
from app.providers.bedrock import BedrockProvider
from app.providers.circuit_breaker import CircuitBreaker, CircuitState
from app.providers.mock import MockProvider
from app.providers.vertex import VertexProvider
from app.routing.engine import RoutingEngine
from app.routing.policies import ordered_candidates


def _request(text: str = "hello", model: str = "gpt-proxy") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=[ChatMessage(role="user", content=text)],
    )


def test_prefer_cost_orders_cheapest_first() -> None:
    settings = Settings(
        provider_cost_per_1k_input="mock:0.0,bedrock:1.0,vertex:0.1",
        provider_cost_per_1k_output="mock:0.0,bedrock:1.0,vertex:0.1",
        routing_policy="prefer_cost",
    )
    providers = {
        "bedrock": MockProvider(latency_ms=0, name="bedrock"),
        "vertex": MockProvider(latency_ms=0, name="vertex"),
        "mock": MockProvider(latency_ms=0, name="mock"),
    }
    ranked, reason = ordered_candidates(_request(), providers, settings)
    assert reason == "cost"
    assert [p.name for p in ranked] == ["mock", "vertex", "bedrock"]


def test_prefer_latency_orders_fastest_first() -> None:
    settings = Settings(
        provider_latency_ms="mock:40,bedrock:200,vertex:150",
        routing_policy="prefer_latency",
    )
    providers = {
        "bedrock": MockProvider(latency_ms=0, name="bedrock"),
        "vertex": MockProvider(latency_ms=0, name="vertex"),
        "mock": MockProvider(latency_ms=0, name="mock"),
    }
    ranked, reason = ordered_candidates(_request(), providers, settings)
    assert reason == "latency"
    assert [p.name for p in ranked] == ["mock", "vertex", "bedrock"]


def test_prefer_provider_uses_primary_then_fallback() -> None:
    settings = Settings(
        routing_policy="prefer_provider",
        routing_primary="bedrock",
        routing_fallback="vertex,mock",
    )
    providers = {
        "mock": MockProvider(latency_ms=0, name="mock"),
        "vertex": MockProvider(latency_ms=0, name="vertex"),
        "bedrock": MockProvider(latency_ms=0, name="bedrock"),
    }
    ranked, reason = ordered_candidates(_request(), providers, settings)
    assert reason == "affinity"
    assert [p.name for p in ranked] == ["bedrock", "vertex", "mock"]


@pytest.mark.asyncio
async def test_failover_on_primary_failure() -> None:
    settings = Settings(
        routing_policy="failover",
        routing_primary="bedrock",
        routing_fallback="vertex,mock",
        provider_timeout_ms=1_000,
        circuit_breaker_failure_threshold=5,
    )
    primary = MockProvider(latency_ms=0, name="bedrock", fail_times=1)
    secondary = MockProvider(latency_ms=0, name="vertex")
    engine = RoutingEngine(
        {"bedrock": primary, "vertex": secondary},
        settings=settings,
    )

    decision = await engine.complete(_request())
    assert decision.provider == "vertex"
    assert decision.reason == "failover"
    assert decision.response.route_reason == "failover"
    assert len(decision.attempts) == 1
    assert decision.attempts[0]["provider"] == "bedrock"


@pytest.mark.asyncio
async def test_failover_within_timeout_skips_slow_primary() -> None:
    settings = Settings(
        routing_policy="failover",
        routing_primary="bedrock",
        routing_fallback="mock",
        provider_timeout_ms=50,
        circuit_breaker_failure_threshold=5,
    )

    class SlowProvider(MockProvider):
        async def complete(self, request: ChatCompletionRequest):  # type: ignore[override]
            await asyncio.sleep(0.2)
            return await super().complete(request)

    engine = RoutingEngine(
        {
            "bedrock": SlowProvider(latency_ms=0, name="bedrock"),
            "mock": MockProvider(latency_ms=0, name="mock"),
        },
        settings=settings,
    )
    decision = await engine.complete(_request())
    assert decision.provider == "mock"
    assert any("timed out" in a["error"] for a in decision.attempts)


@pytest.mark.asyncio
async def test_all_providers_failed() -> None:
    settings = Settings(
        routing_policy="failover",
        routing_primary="bedrock",
        routing_fallback="vertex",
        circuit_breaker_failure_threshold=5,
    )
    engine = RoutingEngine(
        {
            "bedrock": MockProvider(latency_ms=0, name="bedrock", fail_times=3),
            "vertex": MockProvider(latency_ms=0, name="vertex", fail_times=3),
        },
        settings=settings,
    )
    with pytest.raises(AllProvidersFailedError) as exc:
        await engine.complete(_request())
    assert len(exc.value.attempts) == 2


def test_circuit_breaker_opens_and_half_opens() -> None:
    breaker = CircuitBreaker(failure_threshold=2, reset_ms=50)
    assert breaker.allow() is True
    breaker.record_failure()
    assert breaker.state == CircuitState.CLOSED
    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN
    assert breaker.allow() is False

    # Force reset window to elapse.
    breaker._opened_at_ms = (breaker._opened_at_ms or 0) - 100  # noqa: SLF001
    assert breaker.allow() is True
    assert breaker.state == CircuitState.HALF_OPEN
    breaker.record_success()
    assert breaker.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_circuit_breaker_skips_open_provider() -> None:
    settings = Settings(
        routing_policy="failover",
        routing_primary="bedrock",
        routing_fallback="mock",
        circuit_breaker_failure_threshold=1,
        circuit_breaker_reset_ms=60_000,
    )
    primary = MockProvider(latency_ms=0, name="bedrock", fail_times=1)
    secondary = MockProvider(latency_ms=0, name="mock")
    engine = RoutingEngine({"bedrock": primary, "mock": secondary}, settings=settings)

    first = await engine.complete(_request("one"))
    assert first.provider == "mock"

    # Primary still failing / circuit open — should go straight to mock.
    primary.reset_failures()
    primary._fail_remaining = 0  # noqa: SLF001 — healthy again, but breaker still open
    second = await engine.complete(_request("two"))
    assert second.provider == "mock"
    assert any("circuit_open" in a["error"] for a in second.attempts)


@pytest.mark.asyncio
async def test_bedrock_adapter_with_mocked_sdk() -> None:
    class FakeBody:
        def read(self) -> bytes:
            return json.dumps(
                {
                    "content": [{"type": "text", "text": "from bedrock"}],
                    "usage": {"input_tokens": 3, "output_tokens": 2},
                }
            ).encode("utf-8")

    class FakeClient:
        def invoke_model(self, **kwargs: Any) -> dict[str, Any]:
            assert "modelId" in kwargs
            body = json.loads(kwargs["body"])
            assert body["messages"][0]["content"] == "hi bedrock"
            return {"body": FakeBody()}

    provider = BedrockProvider(client=FakeClient())
    result = await provider.complete(_request("hi bedrock"))
    assert result.provider == "bedrock"
    assert result.choices[0].message.content == "from bedrock"
    assert result.usage.total_tokens == 5
    assert provider.estimate_cost(_request()).total_usd >= 0


@pytest.mark.asyncio
async def test_vertex_adapter_with_mocked_sdk() -> None:
    class FakeModel:
        def generate_content(self, contents: str, **kwargs: Any) -> Any:
            assert "user: hi vertex" in contents
            return SimpleNamespace(
                text="from vertex",
                usage_metadata=SimpleNamespace(prompt_token_count=4, candidates_token_count=3),
            )

    provider = VertexProvider(model_factory=lambda _name: FakeModel())
    result = await provider.complete(_request("hi vertex"))
    assert result.provider == "vertex"
    assert result.choices[0].message.content == "from vertex"
    assert result.usage.prompt_tokens == 4
    assert result.usage.completion_tokens == 3


@pytest.mark.asyncio
async def test_bedrock_maps_sdk_errors() -> None:
    class BoomClient:
        def invoke_model(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("AccessDenied")

    provider = BedrockProvider(client=BoomClient())
    with pytest.raises(ProviderError) as exc:
        await provider.complete(_request())
    assert exc.value.provider == "bedrock"
    assert "AccessDenied" in str(exc.value)


def test_model_map_resolution() -> None:
    settings = Settings(
        model_map="gpt-proxy=bedrock:anthropic.claude-3-haiku-20240307-v1:0,vertex:gemini-1.5-flash"
    )
    assert (
        settings.resolve_physical_model("gpt-proxy", "bedrock")
        == "anthropic.claude-3-haiku-20240307-v1:0"
    )
    assert settings.resolve_physical_model("gpt-proxy", "vertex") == "gemini-1.5-flash"
    assert settings.resolve_physical_model("unknown", "mock") == "unknown"
