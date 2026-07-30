from __future__ import annotations

import json
from typing import Any
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import app
from app.models import ChatCompletionRequest, ChatMessage
from app.providers.bedrock import BedrockProvider
from app.providers.mock import MockProvider
from app.providers.vertex import VertexProvider
from app.routing.engine import RoutingEngine


def _parse_sse(body: str) -> list[dict[str, Any] | str]:
    events: list[dict[str, Any] | str] = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block.startswith("data:"):
            continue
        data = block[len("data:") :].strip()
        if data == "[DONE]":
            events.append("[DONE]")
            continue
        events.append(json.loads(data))
    return events


def test_stream_returns_sse_chunks(client: TestClient) -> None:
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "mock-small",
            "messages": [{"role": "user", "content": "hello stream"}],
            "stream": True,
        },
        headers={"X-Cache-Bypass": "1"},
    ) as res:
        assert res.status_code == 200
        assert "text/event-stream" in res.headers["content-type"]
        body = "".join(res.iter_text())

    events = _parse_sse(body)
    assert events[-1] == "[DONE]"
    chunks = [e for e in events if isinstance(e, dict)]
    assert chunks[0]["object"] == "chat.completion.chunk"
    assert chunks[0]["choices"][0]["delta"].get("role") == "assistant"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    content = "".join(
        c["choices"][0]["delta"].get("content", "") for c in chunks if isinstance(c, dict)
    )
    assert "hello stream" in content
    assert any(c.get("provider") == "mock" for c in chunks)


def test_non_stream_still_works(client: TestClient) -> None:
    res = client.post(
        "/v1/chat/completions",
        json={
            "model": "mock-small",
            "messages": [{"role": "user", "content": "still sync"}],
        },
        headers={"X-Cache-Bypass": "1"},
    )
    assert res.status_code == 200
    assert res.json()["object"] == "chat.completion"
    assert "still sync" in res.json()["choices"][0]["message"]["content"]


def test_stream_cache_stores_only_after_complete(client: TestClient) -> None:
    tenant = "stream-cache-tenant"
    prompt = "Unique stream cache prompt xyz-4242"

    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "mock-small",
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
        },
        headers={"X-Tenant-Id": tenant},
    ) as res:
        assert res.status_code == 200
        body = "".join(res.iter_text())
    assert "[DONE]" in body

    # Second stream should be served from cache.
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "mock-small",
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
        },
        headers={"X-Tenant-Id": tenant},
    ) as res:
        assert res.status_code == 200
        body2 = "".join(res.iter_text())

    events = _parse_sse(body2)
    chunks = [e for e in events if isinstance(e, dict)]
    assert any(c.get("cached") is True for c in chunks)
    assert any(c.get("route_reason") == "cache_hit" for c in chunks)


@pytest.mark.asyncio
async def test_stream_disconnect_cancels_upstream() -> None:
    """Closing the client mid-stream should stop consuming provider deltas."""
    provider = MockProvider(latency_ms=50, name="mock")
    engine = RoutingEngine(
        {"mock": provider},
        settings=Settings(routing_policy="failover", routing_primary="mock"),
    )

    # Drive open_stream + partial consume, then aclose (simulates disconnect cleanup).
    route = await engine.open_stream(
        ChatCompletionRequest(
            model="mock-small",
            messages=[ChatMessage(role="user", content="abort me please now")],
            stream=True,
        )
    )
    first = await route.deltas.__anext__()
    assert isinstance(first, str) and first
    await route.aclose()


@pytest.mark.asyncio
async def test_stream_failover_before_first_byte() -> None:
    settings = Settings(
        routing_policy="failover",
        routing_primary="bedrock",
        routing_fallback="mock",
        circuit_breaker_failure_threshold=10,
    )
    primary = MockProvider(latency_ms=0, fail_times=1, name="bedrock")
    secondary = MockProvider(latency_ms=0, name="mock")
    engine = RoutingEngine({"bedrock": primary, "mock": secondary}, settings=settings)
    route = await engine.open_stream(_request("failover stream"))
    assert route.provider == "mock"
    text = ""
    async for delta in route.deltas:
        text += delta
    assert "failover stream" in text
    await route.aclose()


def _request(text: str = "hello", model: str = "gpt-proxy") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=[ChatMessage(role="user", content=text)],
        stream=True,
    )


@pytest.mark.asyncio
async def test_bedrock_stream_with_mocked_sdk() -> None:
    events = [
        {
            "chunk": {
                "bytes": json.dumps(
                    {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "Hello"},
                    }
                ).encode()
            }
        },
        {
            "chunk": {
                "bytes": json.dumps(
                    {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": " Bedrock"},
                    }
                ).encode()
            }
        },
    ]

    class FakeClient:
        def invoke_model(self, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError("complete path should not run")

        def invoke_model_with_response_stream(self, **kwargs: Any) -> dict[str, Any]:
            assert "modelId" in kwargs
            return {"body": iter(events)}

    provider = BedrockProvider(client=FakeClient())
    parts: list[str] = []
    async for delta in provider.stream(_request("hi")):
        parts.append(delta)
    assert "".join(parts) == "Hello Bedrock"


@pytest.mark.asyncio
async def test_vertex_stream_with_mocked_sdk() -> None:
    class FakeModel:
        def generate_content(self, contents: str, **kwargs: Any) -> Any:
            assert kwargs.get("stream") is True
            return iter(
                [
                    SimpleNamespace(text="Hello"),
                    SimpleNamespace(text=" Vertex"),
                ]
            )

    provider = VertexProvider(model_factory=lambda _name: FakeModel())
    parts: list[str] = []
    async for delta in provider.stream(_request("hi")):
        parts.append(delta)
    assert "".join(parts) == "Hello Vertex"


@pytest.mark.asyncio
async def test_http_disconnect_stops_generator() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        async with ac.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "mock-small",
                "messages": [{"role": "user", "content": "disconnect please"}],
                "stream": True,
            },
            headers={"X-Cache-Bypass": "1"},
        ) as res:
            assert res.status_code == 200
            # Read only the first chunk, then close — mirrors client abort.
            async for _line in res.aiter_lines():
                break
