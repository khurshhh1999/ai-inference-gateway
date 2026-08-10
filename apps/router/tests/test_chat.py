from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.models import ChatCompletionRequest, ChatMessage
from app.providers import reset_routing_engine
from app.providers.mock import MockProvider


@pytest.fixture(autouse=True)
def _reset_engine() -> None:
    reset_routing_engine()
    yield
    reset_routing_engine()


def test_health(client: TestClient) -> None:
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["service"] == "router"
    assert body["providers"]["mock"] is True
    assert "routing_policy" in body


def test_chat_completions_mock(client: TestClient) -> None:
    res = client.post(
        "/v1/chat/completions",
        json={
            "model": "mock-small",
            "messages": [{"role": "user", "content": "hello gateway"}],
        },
        headers={"X-Request-Id": "router-test-req-001"},
    )
    assert res.status_code == 200
    assert res.headers.get("x-request-id") == "router-test-req-001"
    body = res.json()
    assert body["object"] == "chat.completion"
    assert body["provider"] == "mock"
    assert body["cached"] is False
    assert body["route_reason"] in {"failover", "cost", "latency", "affinity"}
    assert "hello gateway" in body["choices"][0]["message"]["content"]
    assert body["usage"]["total_tokens"] >= 2


def test_health_includes_otel(client: TestClient) -> None:
    res = client.get("/health")
    assert res.status_code == 200
    otel = res.json()["otel"]
    assert otel["enabled"] is True
    assert otel["service_name"] == "router"
    assert "otlp_configured" in otel


def test_chat_completions_validation(client: TestClient) -> None:
    res = client.post(
        "/v1/chat/completions",
        json={"model": "mock-small", "messages": []},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_mock_provider_direct() -> None:
    provider = MockProvider(latency_ms=0)
    result = await provider.complete(
        ChatCompletionRequest(
            model="mock-small",
            messages=[ChatMessage(role="user", content="ping")],
        )
    )
    assert result.provider == "mock"
    assert "ping" in result.choices[0].message.content
    estimate = provider.estimate_cost(
        ChatCompletionRequest(
            model="mock-small",
            messages=[ChatMessage(role="user", content="ping")],
        )
    )
    assert estimate.total_usd == 0.0
