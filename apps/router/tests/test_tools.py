from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.models import ChatCompletionRequest, ChatMessage, FunctionDef, ToolSpec
from app.providers.mock import MockProvider

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Look up the weather for a city",
        "parameters": {
            "type": "object",
            "properties": {"location": {"type": "string"}},
            "required": ["location"],
        },
    },
}


def test_mock_auto_calls_named_tool(client: TestClient) -> None:
    res = client.post(
        "/v1/chat/completions",
        json={
            "model": "mock-small",
            "messages": [
                {"role": "user", "content": "Call get_weather for Boston"},
            ],
            "tools": [WEATHER_TOOL],
        },
        headers={"X-Cache-Bypass": "1"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    call = body["choices"][0]["message"]["tool_calls"][0]
    assert call["type"] == "function"
    assert call["function"]["name"] == "get_weather"
    args = json.loads(call["function"]["arguments"])
    assert "Boston" in args["location"]
    assert body["choices"][0]["message"]["content"] is None
    assert body["cached"] is False


def test_mock_required_calls_first_tool(client: TestClient) -> None:
    res = client.post(
        "/v1/chat/completions",
        json={
            "model": "mock-small",
            "messages": [{"role": "user", "content": "do something useful"}],
            "tools": [WEATHER_TOOL],
            "tool_choice": "required",
        },
        headers={"X-Cache-Bypass": "1"},
    )
    assert res.status_code == 200
    assert res.json()["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "get_weather"


def test_mock_tool_choice_none_echoes(client: TestClient) -> None:
    res = client.post(
        "/v1/chat/completions",
        json={
            "model": "mock-small",
            "messages": [{"role": "user", "content": "Call get_weather for Boston"}],
            "tools": [WEATHER_TOOL],
            "tool_choice": "none",
        },
        headers={"X-Cache-Bypass": "1"},
    )
    assert res.status_code == 200
    message = res.json()["choices"][0]["message"]
    assert message.get("tool_calls") in (None, [])
    assert "Call get_weather" in message["content"]


def test_mock_tool_followup_returns_text(client: TestClient) -> None:
    first = client.post(
        "/v1/chat/completions",
        json={
            "model": "mock-small",
            "messages": [{"role": "user", "content": "Call get_weather for Boston"}],
            "tools": [WEATHER_TOOL],
        },
        headers={"X-Cache-Bypass": "1"},
    )
    call = first.json()["choices"][0]["message"]["tool_calls"][0]
    res = client.post(
        "/v1/chat/completions",
        json={
            "model": "mock-small",
            "messages": [
                {"role": "user", "content": "Call get_weather for Boston"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [call],
                },
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": '{"temp_f": 72, "conditions": "sunny"}',
                },
            ],
            "tools": [WEATHER_TOOL],
        },
        headers={"X-Cache-Bypass": "1"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["choices"][0]["finish_reason"] == "stop"
    assert "72" in body["choices"][0]["message"]["content"]
    assert body["choices"][0]["message"].get("tool_calls") in (None, [])


def test_tools_are_not_cached(client: TestClient) -> None:
    payload = {
        "model": "mock-small",
        "messages": [{"role": "user", "content": "Call get_weather for Boston"}],
        "tools": [WEATHER_TOOL],
    }
    first = client.post("/v1/chat/completions", json=payload)
    second = client.post("/v1/chat/completions", json=payload)
    assert first.json()["cached"] is False
    assert second.json()["cached"] is False
    assert first.json()["choices"][0]["finish_reason"] == "tool_calls"
    assert second.json()["choices"][0]["finish_reason"] == "tool_calls"


def test_stream_tools_emits_tool_call_chunks(client: TestClient) -> None:
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "mock-small",
            "messages": [{"role": "user", "content": "Call get_weather for Paris"}],
            "tools": [WEATHER_TOOL],
            "stream": True,
        },
        headers={"X-Cache-Bypass": "1"},
    ) as res:
        assert res.status_code == 200
        body = "".join(res.iter_text())
    assert "tool_calls" in body
    assert "get_weather" in body
    assert "[DONE]" in body
    assert "finish_reason" in body


def test_tool_message_requires_tool_call_id(client: TestClient) -> None:
    res = client.post(
        "/v1/chat/completions",
        json={
            "model": "mock-small",
            "messages": [{"role": "tool", "content": "ok"}],
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_mock_prompt_tokens_tolerate_null_content() -> None:
    provider = MockProvider(latency_ms=0)
    result = await provider.complete(
        ChatCompletionRequest(
            model="mock-small",
            messages=[ChatMessage(role="user", content="Call get_weather now")],
            tools=[ToolSpec(function=FunctionDef(name="get_weather"))],
            tool_choice="required",
        )
    )
    assert result.usage.prompt_tokens >= 1
    assert result.choices[0].message.tool_calls
