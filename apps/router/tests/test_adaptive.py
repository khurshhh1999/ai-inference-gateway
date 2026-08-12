from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

import app.api.chat as chat_api
from app.config import Settings
from app.main import app
from app.models import ChatCompletionRequest, ChatMessage
from app.providers import reset_routing_engine
from app.providers.mock import MockProvider
from app.routing.engine import RoutingEngine
from app.routing.policies import ordered_candidates
from app.routing.signals import AdaptiveSignals


def _request(text: str = "hello", model: str = "gpt-proxy") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=[ChatMessage(role="user", content=text)],
    )


def _adaptive_settings(**kwargs: object) -> Settings:
    base: dict[str, object] = {
        "routing_policy": "adaptive",
        "provider_latency_ms": "slow:40,fast:40,flaky:40,stable:40",
        "adaptive_ewma_alpha": 0.5,
        "adaptive_error_penalty_ms": 1_000.0,
        "adaptive_min_samples": 1,
        "adaptive_stale_after_seconds": 3_600.0,
        "provider_timeout_ms": 500,
        "circuit_breaker_failure_threshold": 99,
        "circuit_breaker_reset_ms": 60_000,
    }
    base.update(kwargs)
    return Settings(**base)  # type: ignore[arg-type]


def test_ewma_updates_latency_and_error_rate() -> None:
    sig = AdaptiveSignals(alpha=0.5, error_penalty_ms=1_000.0, latency_hints_ms={"a": 100})
    assert sig.is_cold("a")
    assert sig.latency_ms("a") == 100
    assert sig.score("a") == 100

    sig.observe("a", 20.0, error=False)
    assert sig.latency_ms("a") == pytest.approx(20.0)
    assert sig.error_rate("a") == pytest.approx(0.0)

    sig.observe("a", 40.0, error=True)
    assert sig.latency_ms("a") == pytest.approx(0.5 * 40 + 0.5 * 20)
    assert sig.error_rate("a") == pytest.approx(0.5)
    assert sig.score("a") == pytest.approx(30.0 + 1_000.0 * 0.5)


def test_adaptive_cold_start_matches_latency_hints() -> None:
    settings = Settings(
        routing_policy="adaptive",
        provider_latency_ms="mock:40,bedrock:200,vertex:150",
    )
    providers = {
        "bedrock": MockProvider(latency_ms=0, name="bedrock"),
        "vertex": MockProvider(latency_ms=0, name="vertex"),
        "mock": MockProvider(latency_ms=0, name="mock"),
    }
    ranked, reason = ordered_candidates(_request(), providers, settings)
    assert reason == "adaptive"
    assert [p.name for p in ranked] == ["mock", "vertex", "bedrock"]


def test_unknown_policy_falls_back_to_failover() -> None:
    settings = Settings(routing_policy="not-a-policy", routing_primary="bedrock")
    providers = {
        "mock": MockProvider(latency_ms=0, name="mock"),
        "bedrock": MockProvider(latency_ms=0, name="bedrock"),
    }
    ranked, reason = ordered_candidates(_request(), providers, settings)
    assert reason == "failover"
    assert [p.name for p in ranked] == ["bedrock", "mock"]


@pytest.mark.asyncio
async def test_adaptive_demotes_slow_provider() -> None:
    settings = _adaptive_settings()
    slow = MockProvider(latency_ms=80, name="slow")
    fast = MockProvider(latency_ms=0, name="fast")
    engine = RoutingEngine({"slow": slow, "fast": fast}, settings=settings)

    first = await engine.complete(_request("warm-slow"))
    assert first.reason == "adaptive"
    # Equal hints → insertion order, slow is tried first and succeeds.
    assert first.provider == "slow"

    later = [await engine.complete(_request(f"n{i}")) for i in range(4)]
    assert all(d.reason == "adaptive" for d in later)
    assert later[-1].provider == "fast"
    assert later[-1].attempts == []


@pytest.mark.asyncio
async def test_adaptive_demotes_erroring_provider() -> None:
    settings = _adaptive_settings()
    flaky = MockProvider(latency_ms=0, name="flaky", fail_times=3)
    stable = MockProvider(latency_ms=0, name="stable")
    engine = RoutingEngine({"flaky": flaky, "stable": stable}, settings=settings)

    first = await engine.complete(_request("boom"))
    assert first.provider == "stable"
    assert first.reason == "adaptive"
    assert any(a["provider"] == "flaky" for a in first.attempts)

    second = await engine.complete(_request("again"))
    assert second.provider == "stable"
    assert second.reason == "adaptive"
    assert second.attempts == []


@pytest.mark.asyncio
async def test_adaptive_stream_follows_live_signals() -> None:
    settings = _adaptive_settings()
    flaky = MockProvider(latency_ms=0, name="flaky", fail_times=2)
    stable = MockProvider(latency_ms=0, name="stable")
    engine = RoutingEngine({"flaky": flaky, "stable": stable}, settings=settings)

    first = await engine.open_stream(_request("stream-1"))
    assert first.reason == "adaptive"
    assert first.provider == "stable"
    await first.aclose()

    second = await engine.open_stream(_request("stream-2"))
    assert second.provider == "stable"
    assert second.attempts == []
    await second.aclose()


def test_stale_provider_falls_back_to_hint() -> None:
    sig = AdaptiveSignals(
        alpha=0.5,
        error_penalty_ms=1_000.0,
        stale_after_seconds=0.05,
        latency_hints_ms={"flaky": 40, "stable": 80},
    )
    sig.observe("flaky", 5.0, error=True)
    assert sig.score("flaky") > sig.score("stable")
    sig._last_ts["flaky"] = time.monotonic() - 1.0
    assert sig.is_stale("flaky")
    assert sig.is_cold("flaky")
    assert sig.score("flaky") == 40
    assert sig.score("stable") == 80


@pytest.mark.asyncio
async def test_stale_erroring_provider_can_be_retried() -> None:
    settings = _adaptive_settings(adaptive_stale_after_seconds=0.05)
    flaky = MockProvider(latency_ms=0, name="flaky", fail_times=1)
    stable = MockProvider(latency_ms=0, name="stable")
    engine = RoutingEngine({"flaky": flaky, "stable": stable}, settings=settings)

    first = await engine.complete(_request("fail-once"))
    assert first.provider == "stable"
    assert any(a["provider"] == "flaky" for a in first.attempts)

    await asyncio.sleep(0.06)
    # Stale → hints (equal 40ms) → insertion order tries flaky again; it now succeeds.
    flaky.reset_failures()
    flaky._fail_remaining = 0
    probed = await engine.complete(_request("probe"))
    assert probed.provider == "flaky"
    assert probed.attempts == []


def test_routing_stats_and_health_expose_adaptive(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_routing_engine()
    settings = _adaptive_settings(provider_latency_ms="bedrock:10,mock:200")
    engine = RoutingEngine(
        {
            "bedrock": MockProvider(latency_ms=0, name="bedrock", fail_times=1),
            "mock": MockProvider(latency_ms=0, name="mock"),
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
        json={"model": "gpt-proxy", "messages": [{"role": "user", "content": "adapt"}]},
        headers={"X-Cache-Bypass": "1"},
    )
    assert chat.status_code == 200
    body = chat.json()
    assert body["route_reason"] == "adaptive"
    assert body["provider"] == "mock"

    stats = client.get("/v1/routing/stats")
    assert stats.status_code == 200
    payload = stats.json()
    assert payload["policy"] == "adaptive"
    assert "bedrock" in payload["providers"]
    assert payload["providers"]["bedrock"]["ewma_error_rate"] > 0

    health = client.get("/health")
    assert health.status_code == 200
    assert "adaptive" in health.json()
    assert "providers" in health.json()["adaptive"]

    metrics = client.get("/metrics")
    assert "router_adaptive_latency_ewma_seconds" in metrics.text
    assert "router_adaptive_error_rate" in metrics.text
    assert "router_adaptive_score_seconds" in metrics.text
    assert 'reason="adaptive"' in metrics.text or "reason='adaptive'" in metrics.text
