from __future__ import annotations

import re
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis

from app.cache.embeddings import HashingEmbedder, cosine_similarity
from app.cache.index_backend import (
    pack_f32,
    redis_supports_search,
    resolve_index_backend,
    unpack_f32,
)
from app.cache.metrics import CacheMetrics
from app.cache.semantic import SemanticCache
from app.models import ChatChoice, ChatChoiceMessage, ChatCompletionResponse, Usage


def _as_str(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


class FakeSearchRedis(FakeAsyncRedis):
    """fakeredis plus a tiny RediSearch VECTOR subset (FT.CREATE / FT.SEARCH KNN)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.ft_indexes: dict[str, dict[str, Any]] = {}

    async def execute_command(self, *args: Any, **kwargs: Any) -> Any:
        cmd = _as_str(args[0]).upper() if args else ""
        if cmd == "MODULE" and len(args) > 1 and _as_str(args[1]).upper() == "LIST":
            return [[b"name", b"search", b"ver", 20811]]
        if cmd == "FT.CREATE":
            return self._ft_create(args)
        if cmd == "FT.SEARCH":
            return await self._ft_search(args)
        return await super().execute_command(*args, **kwargs)

    def _ft_create(self, args: tuple[Any, ...]) -> bytes:
        tokens = [_as_str(a) for a in args]
        name = tokens[1]
        prefix = ""
        dim = 256
        for i, tok in enumerate(tokens):
            upper = tok.upper()
            if upper == "PREFIX" and i + 2 < len(tokens):
                prefix = tokens[i + 2]
            if upper == "DIM" and i + 1 < len(tokens):
                dim = int(tokens[i + 1])
        self.ft_indexes[name] = {"prefix": prefix, "dim": dim}
        return b"OK"

    async def _ft_search(self, args: tuple[Any, ...]) -> list[Any]:
        name = _as_str(args[1])
        spec = self.ft_indexes.get(name)
        if spec is None:
            from redis.exceptions import ResponseError

            raise ResponseError("Unknown index name")
        query = _as_str(args[2])
        match = re.search(r"KNN\s+(\d+)", query)
        k = int(match.group(1)) if match else 10
        blob: bytes | None = None
        for i, arg in enumerate(args):
            if _as_str(arg).lower() == "vec" and i + 1 < len(args):
                raw = args[i + 1]
                blob = raw if isinstance(raw, bytes) else bytes(raw)
                break
        if blob is None:
            return [0]
        query_vec = unpack_f32(blob, spec["dim"])
        prefix = spec["prefix"]
        scored: list[tuple[float, str, dict[Any, Any]]] = []
        keys = await self.keys(f"{prefix}*")
        for key in keys:
            key_s = _as_str(key)
            data = await self.hgetall(key)
            fields = {_as_str(fk): fv for fk, fv in data.items()}
            emb_raw = fields.get("embedding")
            if not isinstance(emb_raw, (bytes, bytearray)):
                continue
            stored = unpack_f32(bytes(emb_raw), spec["dim"])
            dist = 1.0 - cosine_similarity(query_vec, stored)
            scored.append((dist, key_s, fields))
        scored.sort(key=lambda row: row[0])
        top = scored[:k]
        out: list[Any] = [len(top)]
        for dist, key_s, fields in top:
            out.append(key_s.encode() if not isinstance(key_s, bytes) else key_s)
            out.append(
                [
                    b"prompt",
                    fields.get("prompt", b""),
                    b"response",
                    fields.get("response", b""),
                    b"cost_usd",
                    fields.get("cost_usd", b"0"),
                    b"dist",
                    str(dist).encode(),
                ]
            )
        return out


def _completion(content: str = "cached answer") -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="chatcmpl-1",
        created=1,
        model="mock-small",
        choices=[ChatChoice(message=ChatChoiceMessage(content=content))],
        usage=Usage(prompt_tokens=3, completion_tokens=2, total_tokens=5),
        provider="mock",
        cached=False,
        route_reason="failover",
    )


def test_pack_f32_roundtrip() -> None:
    vec = [0.1, -0.2, 0.3, 0.0]
    assert unpack_f32(pack_f32(vec), 4) == pytest.approx(vec)


@pytest.mark.asyncio
async def test_auto_falls_back_to_scan_without_search_module() -> None:
    client = FakeAsyncRedis(decode_responses=False)
    assert await redis_supports_search(client) is False
    backend = await resolve_index_backend(
        client, "auto", dim=256, ann_top_k=25, ann_ef_runtime=64
    )
    assert backend.name == "scan"


@pytest.mark.asyncio
async def test_redisearch_forced_falls_back_without_module() -> None:
    client = FakeAsyncRedis(decode_responses=False)
    backend = await resolve_index_backend(
        client, "redisearch", dim=256, ann_top_k=25, ann_ef_runtime=64
    )
    assert backend.name == "scan"


@pytest.mark.asyncio
async def test_redisearch_near_duplicate_hit_and_tenant_isolation() -> None:
    metrics = CacheMetrics()
    cache = SemanticCache(
        FakeSearchRedis(decode_responses=False),
        HashingEmbedder(dim=256),
        similarity_threshold=0.90,
        ttl_seconds=60,
        max_entries=10,
        metrics=metrics,
        index_backend="redisearch",
        ann_top_k=10,
    )
    prompt = "Explain semantic caching in one sentence."
    await cache.store(
        tenant="acme",
        model="mock-small",
        prompt=prompt,
        response=_completion(),
        cost_usd=0.01,
    )
    assert cache.index_backend_name == "redisearch"

    hit = await cache.lookup(
        tenant="acme",
        model="mock-small",
        prompt="Explain semantic caching in a single sentence.",
    )
    assert hit is not None
    assert hit.response.cached is True
    assert hit.response.route_reason == "cache_hit"
    assert hit.response.choices[0].message.content == "cached answer"
    assert hit.saved_usd == pytest.approx(0.01)
    assert metrics.cache_hit_total == 1

    cross = await cache.lookup(
        tenant="other",
        model="mock-small",
        prompt="Explain semantic caching in a single sentence.",
    )
    assert cross is None
    assert metrics.cache_miss_total == 1


@pytest.mark.asyncio
async def test_redisearch_limits_candidates_to_top_k() -> None:
    cache = SemanticCache(
        FakeSearchRedis(decode_responses=False),
        HashingEmbedder(dim=256),
        similarity_threshold=0.99,
        ttl_seconds=60,
        max_entries=100,
        metrics=CacheMetrics(),
        index_backend="redisearch",
        ann_top_k=5,
    )
    base = _completion("x")
    for i in range(20):
        await cache.store(
            tenant="t",
            model="mock-small",
            prompt=f"totally unique prompt number {i} xyz{i}",
            response=base.model_copy(update={"id": f"chatcmpl-{i}"}),
            cost_usd=0.001,
        )
    backend = await cache._ensure_backend()
    query = cache.embed_prompt("totally unique prompt number 19 xyz19")
    found = await backend.candidates(
        "sc:t:mock-small",
        query,
        top_k=5,
        ef_runtime=64,
    )
    assert 1 <= len(found) <= 5


@pytest.mark.asyncio
async def test_redisearch_max_entries_eviction() -> None:
    cache = SemanticCache(
        FakeSearchRedis(decode_responses=False),
        HashingEmbedder(dim=256),
        similarity_threshold=0.99,
        ttl_seconds=60,
        max_entries=2,
        metrics=CacheMetrics(),
        index_backend="redisearch",
    )
    base = _completion("x")
    for i in range(3):
        await cache.store(
            tenant="t",
            model="mock-small",
            prompt=f"totally unique prompt number {i} xyz{i}",
            response=base.model_copy(update={"id": f"chatcmpl-{i}"}),
            cost_usd=0.001,
        )
    size = await cache._redis.zcard("sc:t:mock-small:index")
    assert size == 2
