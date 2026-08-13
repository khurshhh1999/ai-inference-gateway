from __future__ import annotations

import json
import logging
import struct
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import redis.asyncio as redis
from redis.exceptions import ResponseError

from app.metrics import observe_cache_lookup_candidates, set_cache_index_backend

logger = logging.getLogger(__name__)

SCAN_BACKEND = "scan"
REDISEARCH_BACKEND = "redisearch"
AUTO_BACKEND = "auto"
_VALID_BACKENDS = {SCAN_BACKEND, REDISEARCH_BACKEND, AUTO_BACKEND}


@dataclass
class CacheCandidate:
    """One stored completion considered during lookup."""

    entry_id: str
    prompt: str
    response: dict[str, Any]
    cost_usd: float
    embedding: list[float] | None = None
    cosine: float | None = None


class CacheIndexBackend(ABC):
    """Pluggable lookup/store for the semantic cache namespace."""

    name: str

    @abstractmethod
    async def candidates(
        self,
        ns: str,
        query_embedding: list[float],
        *,
        top_k: int,
        ef_runtime: int,
    ) -> list[CacheCandidate]:
        raise NotImplementedError

    @abstractmethod
    async def upsert(
        self,
        ns: str,
        entry_id: str,
        embedding: list[float],
        payload: dict[str, Any],
        ttl_seconds: int,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def evict_if_needed(self, ns: str, max_entries: int) -> None:
        raise NotImplementedError

    @abstractmethod
    async def drop_ids(self, ns: str, ids: list[str]) -> None:
        raise NotImplementedError


def pack_f32(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack_f32(blob: bytes, dim: int) -> list[float]:
    expected = dim * 4
    if len(blob) != expected:
        raise ValueError(f"embedding blob length {len(blob)} != {expected}")
    return list(struct.unpack(f"<{dim}f", blob))


def _as_str(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _flatten_strings(raw: Any) -> set[str]:
    names: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, bytes):
            names.add(node.decode("utf-8", errors="ignore").lower())
        elif isinstance(node, str):
            names.add(node.lower())
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(raw)
    return names


async def redis_supports_search(client: redis.Redis) -> bool:
    """True when RediSearch / Redis Query Engine is loaded (Redis Stack or Redis 8+)."""
    try:
        raw = await client.execute_command("MODULE", "LIST")
    except Exception:  # noqa: BLE001
        return False
    names = _flatten_strings(raw)
    return "search" in names


def normalize_index_backend(name: str) -> str:
    cleaned = name.strip().lower() or AUTO_BACKEND
    if cleaned not in _VALID_BACKENDS:
        raise ValueError(
            f"Unsupported CACHE_INDEX_BACKEND={name!r}; "
            f"expected one of {sorted(_VALID_BACKENDS)}"
        )
    return cleaned


async def resolve_index_backend(
    client: redis.Redis,
    requested: str,
    *,
    dim: int,
    ann_top_k: int,
    ann_ef_runtime: int,
) -> CacheIndexBackend:
    wanted = normalize_index_backend(requested)
    use_search = wanted == REDISEARCH_BACKEND
    if wanted == AUTO_BACKEND:
        use_search = await redis_supports_search(client)
    elif wanted == REDISEARCH_BACKEND and not await redis_supports_search(client):
        logger.warning(
            "CACHE_INDEX_BACKEND=redisearch but Redis has no search module; "
            "falling back to scan"
        )
        use_search = False

    if use_search:
        backend: CacheIndexBackend = RediSearchIndexBackend(
            client, dim=dim, default_top_k=ann_top_k, ef_runtime=ann_ef_runtime
        )
    else:
        backend = ScanIndexBackend(client)

    set_cache_index_backend(backend.name)
    logger.info("semantic cache index backend=%s (requested=%s)", backend.name, wanted)
    return backend


def _zset_key(ns: str) -> str:
    return f"{ns}:index"


def _json_key(ns: str, entry_id: str) -> str:
    return f"{ns}:e:{entry_id}"


def _hash_key(ns: str, entry_id: str) -> str:
    return f"{ns}:h:{entry_id}"


def _entry_id_from_key(key: str, marker: str) -> str:
    _, _, rest = key.partition(marker)
    return rest or key


async def _evict_oldest(
    client: redis.Redis,
    ns: str,
    max_entries: int,
    delete_key,
) -> None:
    zkey = _zset_key(ns)
    size = await client.zcard(zkey)
    if size <= max_entries:
        return
    overflow = size - max_entries
    old = await client.zrange(zkey, 0, overflow - 1)
    if not old:
        return
    ids = [_as_str(i) for i in old]
    pipe = client.pipeline()
    pipe.zrem(zkey, *ids)
    for entry_id in ids:
        pipe.delete(delete_key(ns, entry_id))
    await pipe.execute()


class ScanIndexBackend(CacheIndexBackend):
    """O(n) namespace scan over JSON blobs. Works on vanilla Redis / fakeredis."""

    name = SCAN_BACKEND

    def __init__(self, client: redis.Redis) -> None:
        self._redis = client

    async def candidates(
        self,
        ns: str,
        query_embedding: list[float],
        *,
        top_k: int,
        ef_runtime: int,
    ) -> list[CacheCandidate]:
        del query_embedding, top_k, ef_runtime
        ids = await self._redis.zrange(_zset_key(ns), 0, -1)
        if not ids:
            return []
        entry_ids = [_as_str(i) for i in ids]
        keys = [_json_key(ns, eid) for eid in entry_ids]
        payloads = await self._redis.mget(keys)
        out: list[CacheCandidate] = []
        stale: list[str] = []
        for entry_id, payload in zip(entry_ids, payloads, strict=True):
            if payload is None:
                stale.append(entry_id)
                continue
            try:
                data = json.loads(_as_str(payload))
            except json.JSONDecodeError:
                stale.append(entry_id)
                continue
            embedding = data.get("embedding")
            if not isinstance(embedding, list):
                stale.append(entry_id)
                continue
            response = data.get("response")
            if not isinstance(response, dict):
                stale.append(entry_id)
                continue
            out.append(
                CacheCandidate(
                    entry_id=entry_id,
                    prompt=str(data.get("prompt") or data.get("prompt_preview") or ""),
                    response=response,
                    cost_usd=float(data.get("cost_usd", 0.0)),
                    embedding=[float(x) for x in embedding],
                )
            )
        if stale:
            await self.drop_ids(ns, stale)
        observe_cache_lookup_candidates(self.name, len(out))
        return out

    async def upsert(
        self,
        ns: str,
        entry_id: str,
        embedding: list[float],
        payload: dict[str, Any],
        ttl_seconds: int,
    ) -> None:
        del embedding
        expire = ttl_seconds if ttl_seconds > 0 else None
        pipe = self._redis.pipeline()
        pipe.set(_json_key(ns, entry_id), json.dumps(payload), ex=expire)
        pipe.zadd(_zset_key(ns), {entry_id: time.time()})
        await pipe.execute()
        if expire:
            await self._redis.expire(_zset_key(ns), ttl_seconds + 60)

    async def evict_if_needed(self, ns: str, max_entries: int) -> None:
        await _evict_oldest(self._redis, ns, max_entries, _json_key)

    async def drop_ids(self, ns: str, ids: list[str]) -> None:
        if not ids:
            return
        pipe = self._redis.pipeline()
        pipe.zrem(_zset_key(ns), *ids)
        for entry_id in ids:
            pipe.delete(_json_key(ns, entry_id))
        await pipe.execute()


class RediSearchIndexBackend(CacheIndexBackend):
    """HNSW KNN via RediSearch / Redis Query Engine (Redis Stack or Redis 8+)."""

    name = REDISEARCH_BACKEND

    def __init__(
        self,
        client: redis.Redis,
        *,
        dim: int,
        default_top_k: int = 25,
        ef_runtime: int = 64,
    ) -> None:
        self._redis = client
        self._dim = dim
        self._default_top_k = max(1, default_top_k)
        self._ef_runtime = max(1, ef_runtime)
        self._ensured: set[str] = set()

    def _index_name(self, ns: str) -> str:
        return f"idx:{ns}:d{self._dim}"

    def _prefix(self, ns: str) -> str:
        return f"{ns}:h:"

    async def _ensure_index(self, ns: str) -> None:
        name = self._index_name(ns)
        if name in self._ensured:
            return
        try:
            await self._redis.execute_command(
                "FT.CREATE",
                name,
                "ON",
                "HASH",
                "PREFIX",
                "1",
                self._prefix(ns),
                "SCHEMA",
                "embedding",
                "VECTOR",
                "HNSW",
                "6",
                "TYPE",
                "FLOAT32",
                "DIM",
                str(self._dim),
                "DISTANCE_METRIC",
                "COSINE",
                "prompt",
                "TEXT",
                "cost_usd",
                "NUMERIC",
            )
        except ResponseError as exc:
            msg = str(exc).lower()
            if "already exists" not in msg:
                raise
        self._ensured.add(name)

    async def candidates(
        self,
        ns: str,
        query_embedding: list[float],
        *,
        top_k: int,
        ef_runtime: int,
    ) -> list[CacheCandidate]:
        await self._ensure_index(ns)
        k = max(1, top_k or self._default_top_k)
        ef = max(1, ef_runtime or self._ef_runtime)
        blob = pack_f32(query_embedding)
        query = f"*=>[KNN {k} @embedding $vec EF_RUNTIME {ef} AS dist]"
        try:
            raw = await self._redis.execute_command(
                "FT.SEARCH",
                self._index_name(ns),
                query,
                "PARAMS",
                "2",
                "vec",
                blob,
                "SORTBY",
                "dist",
                "DIALECT",
                "2",
                "RETURN",
                "4",
                "prompt",
                "response",
                "cost_usd",
                "dist",
            )
        except ResponseError as exc:
            msg = str(exc).lower()
            if "no such index" in msg or "unknown index" in msg:
                observe_cache_lookup_candidates(self.name, 0)
                return []
            raise

        rows = _parse_ft_search(raw)
        out: list[CacheCandidate] = []
        for key, fields in rows:
            entry_id = _entry_id_from_key(key, ":h:")
            response_raw = fields.get("response")
            if response_raw is None:
                continue
            try:
                response = json.loads(_as_str(response_raw))
            except json.JSONDecodeError:
                continue
            if not isinstance(response, dict):
                continue
            dist_raw = fields.get("dist", 1.0)
            try:
                dist = float(_as_str(dist_raw))
            except (TypeError, ValueError):
                continue
            cosine = 1.0 - dist
            cost_raw = fields.get("cost_usd", 0.0)
            try:
                cost_usd = float(_as_str(cost_raw))
            except (TypeError, ValueError):
                cost_usd = 0.0
            out.append(
                CacheCandidate(
                    entry_id=entry_id,
                    prompt=_as_str(fields.get("prompt") or ""),
                    response=response,
                    cost_usd=cost_usd,
                    cosine=cosine,
                )
            )
        observe_cache_lookup_candidates(self.name, len(out))
        return out

    async def upsert(
        self,
        ns: str,
        entry_id: str,
        embedding: list[float],
        payload: dict[str, Any],
        ttl_seconds: int,
    ) -> None:
        await self._ensure_index(ns)
        key = _hash_key(ns, entry_id)
        mapping = {
            "embedding": pack_f32(embedding),
            "prompt": str(payload.get("prompt") or "")[:4000],
            "response": json.dumps(payload.get("response") or {}),
            "cost_usd": str(float(payload.get("cost_usd", 0.0))),
        }
        pipe = self._redis.pipeline()
        pipe.hset(key, mapping=mapping)
        if ttl_seconds > 0:
            pipe.expire(key, ttl_seconds)
        pipe.zadd(_zset_key(ns), {entry_id: time.time()})
        await pipe.execute()
        if ttl_seconds > 0:
            await self._redis.expire(_zset_key(ns), ttl_seconds + 60)

    async def evict_if_needed(self, ns: str, max_entries: int) -> None:
        await _evict_oldest(self._redis, ns, max_entries, _hash_key)

    async def drop_ids(self, ns: str, ids: list[str]) -> None:
        if not ids:
            return
        pipe = self._redis.pipeline()
        pipe.zrem(_zset_key(ns), *ids)
        for entry_id in ids:
            pipe.delete(_hash_key(ns, entry_id))
        await pipe.execute()


def _parse_ft_search(raw: Any) -> list[tuple[str, dict[str, Any]]]:
    if not raw or not isinstance(raw, (list, tuple)):
        return []
    rows = list(raw[1:])
    out: list[tuple[str, dict[str, Any]]] = []
    i = 0
    while i < len(rows):
        key = _as_str(rows[i])
        fields: dict[str, Any] = {}
        if i + 1 < len(rows) and isinstance(rows[i + 1], (list, tuple)):
            pairs = rows[i + 1]
            for j in range(0, len(pairs) - 1, 2):
                fields[_as_str(pairs[j])] = pairs[j + 1]
            i += 2
        else:
            i += 1
        out.append((key, fields))
    return out
