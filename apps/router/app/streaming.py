from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

from app.models import (
    ChatChoice,
    ChatChoiceMessage,
    ChatCompletionResponse,
    Usage,
)


def new_completion_id(provider: str) -> str:
    return f"chatcmpl-{provider}-{uuid.uuid4().hex[:12]}"


def format_sse(payload: dict[str, Any] | str) -> str:
    """Serialize one SSE `data:` frame (OpenAI-compatible)."""
    if isinstance(payload, str):
        data = payload
    else:
        data = json.dumps(payload, separators=(",", ":"))
    return f"data: {data}\n\n"


def chunk_payload(
    *,
    completion_id: str,
    created: int,
    model: str,
    delta: dict[str, Any],
    finish_reason: str | None = None,
    provider: str | None = None,
    cached: bool | None = None,
    route_reason: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    if provider is not None:
        payload["provider"] = provider
    if cached is not None:
        payload["cached"] = cached
    if route_reason is not None:
        payload["route_reason"] = route_reason
    return payload


async def iter_sse_from_text(
    *,
    text: str,
    model: str,
    provider: str,
    completion_id: str | None = None,
    created: int | None = None,
    cached: bool = False,
    route_reason: str | None = None,
    chunk_words: bool = True,
) -> AsyncIterator[str]:
    """Yield SSE frames for a full assistant string (cache hits / fallbacks)."""
    cid = completion_id or new_completion_id(provider)
    ts = created if created is not None else int(time.time())
    yield format_sse(
        chunk_payload(
            completion_id=cid,
            created=ts,
            model=model,
            delta={"role": "assistant", "content": ""},
            provider=provider,
            cached=cached,
            route_reason=route_reason,
        )
    )

    pieces: list[str]
    if chunk_words and text:
        words = text.split(" ")
        pieces = [w if i == len(words) - 1 else w + " " for i, w in enumerate(words)]
    else:
        pieces = [text] if text else []

    for piece in pieces:
        if not piece:
            continue
        yield format_sse(
            chunk_payload(
                completion_id=cid,
                created=ts,
                model=model,
                delta={"content": piece},
                provider=provider,
                cached=cached,
                route_reason=route_reason,
            )
        )

    yield format_sse(
        chunk_payload(
            completion_id=cid,
            created=ts,
            model=model,
            delta={},
            finish_reason="stop",
            provider=provider,
            cached=cached,
            route_reason=route_reason,
        )
    )
    yield format_sse("[DONE]")


async def iter_sse_from_deltas(
    deltas: AsyncIterator[str],
    *,
    model: str,
    provider: str,
    completion_id: str | None = None,
    created: int | None = None,
    cached: bool = False,
    route_reason: str | None = None,
) -> AsyncIterator[tuple[str, str]]:
    """Yield ``(sse_frame, accumulated_text_so_far)`` from provider text deltas."""
    cid = completion_id or new_completion_id(provider)
    ts = created if created is not None else int(time.time())
    accumulated = ""

    yield (
        format_sse(
            chunk_payload(
                completion_id=cid,
                created=ts,
                model=model,
                delta={"role": "assistant", "content": ""},
                provider=provider,
                cached=cached,
                route_reason=route_reason,
            )
        ),
        accumulated,
    )

    async for delta in deltas:
        if not delta:
            continue
        accumulated += delta
        yield (
            format_sse(
                chunk_payload(
                    completion_id=cid,
                    created=ts,
                    model=model,
                    delta={"content": delta},
                    provider=provider,
                    cached=cached,
                    route_reason=route_reason,
                )
            ),
            accumulated,
        )

    yield (
        format_sse(
            chunk_payload(
                completion_id=cid,
                created=ts,
                model=model,
                delta={},
                finish_reason="stop",
                provider=provider,
                cached=cached,
                route_reason=route_reason,
            )
        ),
        accumulated,
    )
    yield (format_sse("[DONE]"), accumulated)


def build_completion_from_stream(
    *,
    completion_id: str,
    created: int,
    model: str,
    content: str,
    provider: str,
    prompt_tokens: int,
    route_reason: str | None = None,
) -> ChatCompletionResponse:
    completion_tokens = max(1, len(content.split())) if content else 1
    return ChatCompletionResponse(
        id=completion_id,
        created=created,
        model=model,
        choices=[ChatChoice(message=ChatChoiceMessage(content=content))],
        usage=Usage(
            prompt_tokens=max(1, prompt_tokens),
            completion_tokens=completion_tokens,
            total_tokens=max(1, prompt_tokens) + completion_tokens,
        ),
        provider=provider,
        cached=False,
        route_reason=route_reason,
    )


async def iterate_sync_iterator(sync_iter: Iterator[Any]) -> AsyncIterator[Any]:
    """Bridge a blocking iterator into async without buffering the whole stream."""
    import asyncio

    sentinel = object()
    it = iter(sync_iter)

    def _next() -> Any:
        return next(it, sentinel)

    while True:
        item = await asyncio.to_thread(_next)
        if item is sentinel:
            break
        yield item
