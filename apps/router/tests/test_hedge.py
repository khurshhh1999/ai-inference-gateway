from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import app.api.chat as chat_api
from app.config import Settings
from app.errors import AllProvidersFailedError
from app.main import app
from app.models import ChatCompletionRequest, ChatMessage
from app.providers import build_providers, reset_routing_engine
from app.providers.circuit_breaker import CircuitState
from app.providers.mock import MockProvider
from app.routing.engine import RoutingEngine


def _request(text: str = "hello", model: str = "gpt-proxy") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=[ChatMessage(role="user", content=text)],
    )


def _hedge_settings(**kwargs: object) -> Settings:
    base: dict[str, object] = {
        "routing_policy": "failover",
        "routing_primary": "slow",
        "routing_fallback": "fast",
        "hedge_after_ms": 40,
        "provider_timeout_ms": 2_000,
        "circuit_breaker_failure_threshold": 99,
        "circuit_breaker_reset_ms": 60_000,
    }
    base.update(kwargs)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_hedge_secondary_wins_when_primary_is_slow() -> None:
    settings = _hedge_settings()
    engine = RoutingEngine(
        {
            "slow": MockProvider(latency_ms=250, name="slow"),
            "fast": MockProvider(latency_ms=10, name="fast"),
        },
        settings=settings,
    )
    started = time.perf_counter()
    decision = await engine.complete(_request("hedge me"))
    elapsed = time.perf_counter() - started
    assert decision.provider == "fast"
    assert decision.reason == "hedged"
    assert decision.hedged is True
    assert decision.response.route_reason == "hedged"
    assert elapsed < 0.20


@pytest.mark.asyncio
async def test_hedge_not_fired_when_primary_is_fast() -> None:
    settings = _hedge_settings(hedge_after_ms=200)
    engine = RoutingEngine(
        {
            "slow": MockProvider(latency_ms=10, name="slow"),
            "fast": MockProvider(latency_ms=10, name="fast"),
        },
        settings=settings,
    )
    decision = await engine.complete(_request("no hedge needed"))
    assert decision.provider == "slow"
    assert decision.reason == "failover"
    assert decision.hedged is False


@pytest.mark.asyncio
async def test_primary_failure_failsover_without_waiting_hedge_delay() -> None:
    settings = _hedge_settings(hedge_after_ms=400)
    engine = RoutingEngine(
        {
            "slow": MockProvider(latency_ms=0, name="slow", fail_times=1),
            "fast": MockProvider(latency_ms=0, name="fast"),
        },
        settings=settings,
    )
    started = time.perf_counter()
    decision = await engine.complete(_request("fail fast"))
    elapsed = time.perf_counter() - started
    assert decision.provider == "fast"
    assert decision.reason == "failover"
    assert decision.hedged is False
    assert elapsed < 0.20
    assert decision.attempts[0]["provider"] == "slow"


@pytest.mark.asyncio
async def test_hedge_disabled_keeps_slow_successful_primary() -> None:
    settings = _hedge_settings(hedge_after_ms=0)
    engine = RoutingEngine(
        {
            "slow": MockProvider(latency_ms=80, name="slow"),
            "fast": MockProvider(latency_ms=5, name="fast"),
        },
        settings=settings,
    )
    decision = await engine.complete(_request("sequential"))
    assert decision.provider == "slow"
    assert decision.reason == "failover"
    assert decision.hedged is False


@pytest.mark.asyncio
async def test_hedge_keeps_primary_when_secondary_fails() -> None:
    settings = _hedge_settings(hedge_after_ms=30)
    engine = RoutingEngine(
        {
            "slow": MockProvider(latency_ms=80, name="slow"),
            "fast": MockProvider(latency_ms=0, name="fast", fail_times=1),
        },
        settings=settings,
    )
    decision = await engine.complete(_request("primary still wins"))
    assert decision.provider == "slow"
    assert decision.reason == "failover"
    assert decision.hedged is False
    assert any(a["provider"] == "fast" for a in decision.attempts)


@pytest.mark.asyncio
async def test_hedge_cancel_does_not_open_circuit() -> None:
    settings = _hedge_settings(hedge_after_ms=25, circuit_breaker_failure_threshold=1)
    slow = MockProvider(latency_ms=80, name="slow")
    fast = MockProvider(latency_ms=400, name="fast")
    engine = RoutingEngine({"slow": slow, "fast": fast}, settings=settings)
    decision = await engine.complete(_request("cancel the hedge"))
    assert decision.provider == "slow"
    assert decision.hedged is False
    assert engine._breakers["fast"].state == CircuitState.CLOSED
    assert engine._breakers["fast"].allow() is True


@pytest.mark.asyncio
async def test_hedge_both_fail_then_third_succeeds() -> None:
    settings = Settings(
        routing_policy="failover",
        routing_primary="a",
        routing_fallback="b,c",
        hedge_after_ms=20,
        provider_timeout_ms=2_000,
        circuit_breaker_failure_threshold=99,
    )
    engine = RoutingEngine(
        {
            "a": MockProvider(latency_ms=80, name="a", fail_times=1),
            "b": MockProvider(latency_ms=0, name="b", fail_times=1),
            "c": MockProvider(latency_ms=0, name="c"),
        },
        settings=settings,
    )
    decision = await engine.complete(_request("third"))
    assert decision.provider == "c"


@pytest.mark.asyncio
async def test_hedge_all_failed() -> None:
    settings = _hedge_settings()
    engine = RoutingEngine(
        {
            "slow": MockProvider(latency_ms=0, name="slow", fail_times=3),
            "fast": MockProvider(latency_ms=0, name="fast", fail_times=3),
        },
        settings=settings,
    )
    with pytest.raises(AllProvidersFailedError):
        await engine.complete(_request("none"))


@pytest.mark.asyncio
async def test_stream_hedge_secondary_wins() -> None:
    settings = _hedge_settings()
    engine = RoutingEngine(
        {
            "slow": MockProvider(latency_ms=250, name="slow"),
            "fast": MockProvider(latency_ms=10, name="fast"),
        },
        settings=settings,
    )
    route = await engine.open_stream(
        ChatCompletionRequest(
            model="gpt-proxy",
            messages=[ChatMessage(role="user", content="stream hedge")],
            stream=True,
        )
    )
    assert route.provider == "fast"
    assert route.reason == "hedged"
    assert route.hedged is True
    text = ""
    async for delta in route.deltas:
        text += delta
    assert "stream hedge" in text
    await route.aclose()


def test_mock_peer_registered_when_configured() -> None:
    settings = Settings(provider_mode="mock", mock_peer_latency_ms=15, mock_latency_ms=80)
    providers = build_providers(settings)
    assert set(providers) == {"mock", "mock-peer"}
    assert providers["mock-peer"].name == "mock-peer"


def test_mock_peer_absent_by_default() -> None:
    settings = Settings(provider_mode="mock", mock_peer_latency_ms=None)
    providers = build_providers(settings)
    assert set(providers) == {"mock"}


def test_empty_mock_peer_env_is_unset() -> None:
    settings = Settings(provider_mode="mock", mock_peer_latency_ms="")  # type: ignore[arg-type]
    assert settings.mock_peer_latency_ms is None
    assert set(build_providers(settings)) == {"mock"}


def test_routing_stats_and_health_expose_hedge(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_routing_engine()
    settings = _hedge_settings(hedge_after_ms=30)
    engine = RoutingEngine(
        {
            "slow": MockProvider(latency_ms=200, name="slow"),
            "fast": MockProvider(latency_ms=5, name="fast"),
        },
        settings=settings,
    )
    monkeypatch.setattr(chat_api, "get_routing_engine", lambda: engine)
    monkeypatch.setattr("app.main.get_routing_engine", lambda: engine)
    monkeypatch.setattr("app.main.settings", settings)
    monkeypatch.setattr(chat_api, "settings", settings)

    client = TestClient(app)
    chat = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-proxy", "messages": [{"role": "user", "content": "hedge http"}]},
        headers={"X-Cache-Bypass": "1"},
    )
    assert chat.status_code == 200
    body = chat.json()
    assert body["route_reason"] == "hedged"
    assert body["provider"] == "fast"

    stats = client.get("/v1/routing/stats")
    assert stats.status_code == 200
    payload = stats.json()
    assert payload["hedge"]["after_ms"] == 30
    assert payload["hedge"]["enabled"] is True

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["hedge"]["after_ms"] == 30

    metrics = client.get("/metrics")
    assert "router_hedge_fired_total" in metrics.text
    assert "router_hedge_won_total" in metrics.text
    assert 'reason="hedged"' in metrics.text or "reason='hedged'" in metrics.text
