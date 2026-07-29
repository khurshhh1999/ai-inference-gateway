from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.providers import reset_routing_engine
from app.providers.mock import MockProvider
from app.routing.engine import RoutingEngine
import app.api.chat as chat_api
import app.main as main_api


def test_chat_endpoint_failsover_and_returns_route_reason(monkeypatch) -> None:
    reset_routing_engine()
    settings = Settings(
        provider_mode="multi",
        routing_policy="failover",
        routing_primary="bedrock",
        routing_fallback="mock",
        provider_timeout_ms=500,
        circuit_breaker_failure_threshold=5,
    )
    engine = RoutingEngine(
        {
            "bedrock": MockProvider(latency_ms=0, name="bedrock", fail_times=1),
            "mock": MockProvider(latency_ms=0, name="mock"),
        },
        settings=settings,
    )
    monkeypatch.setattr(chat_api, "get_routing_engine", lambda: engine)
    monkeypatch.setattr(main_api, "get_routing_engine", lambda: engine)

    client = TestClient(app)
    res = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-proxy",
            "messages": [{"role": "user", "content": "failover please"}],
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["provider"] == "mock"
    assert body["route_reason"] == "failover"
    assert "failover please" in body["choices"][0]["message"]["content"]


def test_chat_endpoint_all_failed_maps_to_502(monkeypatch) -> None:
    reset_routing_engine()
    settings = Settings(
        routing_policy="failover",
        routing_primary="bedrock",
        routing_fallback="vertex",
        circuit_breaker_failure_threshold=5,
    )
    engine = RoutingEngine(
        {
            "bedrock": MockProvider(latency_ms=0, name="bedrock", fail_times=2),
            "vertex": MockProvider(latency_ms=0, name="vertex", fail_times=2),
        },
        settings=settings,
    )
    monkeypatch.setattr(chat_api, "get_routing_engine", lambda: engine)

    client = TestClient(app)
    res = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-proxy",
            "messages": [{"role": "user", "content": "no pe"}],
        },
    )
    assert res.status_code == 502
    detail = res.json()["detail"]
    assert detail["error"] == "all_providers_failed"
    assert len(detail["attempts"]) == 2
