import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import ChatCompletionRequest, ChatMessage
from app.providers.mock import MockProvider


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_health(client: TestClient) -> None:
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["service"] == "router"
    assert body["provider"] == "mock"


def test_chat_completions_mock(client: TestClient) -> None:
    res = client.post(
        "/v1/chat/completions",
        json={
            "model": "mock-small",
            "messages": [{"role": "user", "content": "hello gateway"}],
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["object"] == "chat.completion"
    assert body["provider"] == "mock"
    assert body["cached"] is False
    assert "hello gateway" in body["choices"][0]["message"]["content"]
    assert body["usage"]["total_tokens"] >= 2


def test_chat_completions_validation(client: TestClient) -> None:
    res = client.post(
        "/v1/chat/completions",
        json={"model": "mock-small", "messages": []},
    )
    assert res.status_code == 422


def test_stream_not_implemented(client: TestClient) -> None:
    res = client.post(
        "/v1/chat/completions",
        json={
            "model": "mock-small",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert res.status_code == 501


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
