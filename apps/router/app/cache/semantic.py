from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

import redis.asyncio as redis

from app.cache.embeddings import EmbeddingProvider, combined_similarity, get_embedder
from app.cache.metrics import CacheMetrics, cache_metrics
from app.config import Settings
from app.config import settings as default_settings
from app.models import ChatCompletionResponse

logger = logging.getLogger(__name__)

_cache: SemanticCache | None = None


@dataclass(frozen=True)
class CacheHit:
    response: ChatCompletionResponse
    similarity: float
    saved_usd: float
    entry_id: str


class SemanticCache:
    """Tenant-scoped semantic response cache backed by Redis + cosine similarity.

    Stores embeddings alongside completions under ``sc:{tenant}:{model_family}:*``.
    Lookup scans the namespace (bounded by ``max_entries``) and returns the best
    match above ``similarity_threshold``. Works with vanilla Redis (no RediSearch).
    """

    def __init__(
        self,
        client: redis.Redis,
        embedder: EmbeddingProvider,
        *,
        enabled: bool = True,
        similarity_threshold: float = 0.90,
        ttl_seconds: int = 3600,
        max_entries: int = 1000,
        metrics: CacheMetrics | None = None,
    ) -> None:
        self._redis = client
        self._embedder = embedder
        self.enabled = enabled
        self.similarity_threshold = similarity_threshold
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._metrics = metrics or cache_metrics

    @staticmethod
    def model_family(model: str) -> str:
        # Logical model id is the family key for now (gpt-proxy, mock-small, …).
        return model.strip().lower() or "default"

    @staticmethod
    def _ns(tenant: str, model_family: str) -> str:
        safe_tenant = tenant.strip().lower().replace(" ", "_") or "default"
        safe_family = model_family.strip().lower().replace(" ", "_") or "default"
        return f"sc:{safe_tenant}:{safe_family}"

    def embed_prompt(self, prompt: str) -> list[float]:
        return self._embedder.embed(prompt)

    async def lookup(
        self,
        *,
        tenant: str,
        model: str,
        prompt: str,
    ) -> CacheHit | None:
        if not self.enabled:
            return None

        ns = self._ns(tenant, self.model_family(model))
        query = self.embed_prompt(prompt)
        ids = await self._redis.zrange(f"{ns}:index", 0, -1)
        if not ids:
            self._metrics.record_miss()
            return None

        best: CacheHit | None = None
        stale: list[str] = []

        for raw_id in ids:
            entry_id = raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)
            payload = await self._redis.get(f"{ns}:e:{entry_id}")
            if payload is None:
                stale.append(entry_id)
                continue
            data = json.loads(payload)
            embedding = data.get("embedding")
            stored_prompt = data.get("prompt") or data.get("prompt_preview") or ""
            if not isinstance(embedding, list):
                stale.append(entry_id)
                continue
            try:
                score = combined_similarity(
                    query_embedding=query,
                    stored_embedding=embedding,
                    query_prompt=prompt,
                    stored_prompt=str(stored_prompt),
                )
            except ValueError:
                stale.append(entry_id)
                continue
            if score < self.similarity_threshold:
                continue
            if best is None or score > best.similarity:
                response = ChatCompletionResponse.model_validate(data["response"])
                response.cached = True
                response.route_reason = "cache_hit"
                best = CacheHit(
                    response=response,
                    similarity=score,
                    saved_usd=float(data.get("cost_usd", 0.0)),
                    entry_id=entry_id,
                )

        if stale:
            await self._redis.zrem(f"{ns}:index", *stale)

        if best is None:
            self._metrics.record_miss()
            logger.info(
                "cache miss tenant=%s model=%s candidates=%s",
                tenant,
                model,
                len(ids),
            )
            return None

        self._metrics.record_hit(best.saved_usd)
        logger.info(
            "cache hit tenant=%s model=%s similarity=%.4f saved_usd=%.6f entry=%s",
            tenant,
            model,
            best.similarity,
            best.saved_usd,
            best.entry_id,
        )
        return best

    async def store(
        self,
        *,
        tenant: str,
        model: str,
        prompt: str,
        response: ChatCompletionResponse,
        cost_usd: float,
    ) -> str | None:
        if not self.enabled:
            return None

        ns = self._ns(tenant, self.model_family(model))
        entry_id = uuid.uuid4().hex[:16]
        embedding = self.embed_prompt(prompt)
        # Persist a clean copy — cached flag is set on read, not on write.
        to_store = response.model_copy(deep=True)
        to_store.cached = False
        payload = {
            "embedding": embedding,
            "response": to_store.model_dump(),
            "cost_usd": max(0.0, cost_usd),
            "prompt": prompt[:4000],
            "prompt_preview": prompt[:200],
            "created_at": int(time.time()),
        }
        pipe = self._redis.pipeline()
        pipe.set(
            f"{ns}:e:{entry_id}",
            json.dumps(payload),
            ex=self.ttl_seconds if self.ttl_seconds > 0 else None,
        )
        pipe.zadd(f"{ns}:index", {entry_id: time.time()})
        await pipe.execute()
        await self._evict_if_needed(ns)
        if self.ttl_seconds > 0:
            # Keep the index key from growing forever if entries expire individually.
            await self._redis.expire(f"{ns}:index", self.ttl_seconds + 60)
        logger.info(
            "cache store tenant=%s model=%s entry=%s cost_usd=%.6f",
            tenant,
            model,
            entry_id,
            cost_usd,
        )
        return entry_id

    async def _evict_if_needed(self, ns: str) -> None:
        size = await self._redis.zcard(f"{ns}:index")
        if size <= self.max_entries:
            return
        # Drop oldest (lowest score = earliest insert time).
        overflow = size - self.max_entries
        old = await self._redis.zrange(f"{ns}:index", 0, overflow - 1)
        if not old:
            return
        ids = [i.decode() if isinstance(i, bytes) else str(i) for i in old]
        pipe = self._redis.pipeline()
        pipe.zrem(f"{ns}:index", *ids)
        for entry_id in ids:
            pipe.delete(f"{ns}:e:{entry_id}")
        await pipe.execute()

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:  # noqa: BLE001
            return False

    async def close(self) -> None:
        await self._redis.aclose()


def prompt_from_messages(messages: list[Any]) -> str:
    """Concatenate message contents into a single string for embedding."""
    parts: list[str] = []
    for msg in messages:
        role = getattr(msg, "role", None) or (msg.get("role") if isinstance(msg, dict) else "")
        content = getattr(msg, "content", None) or (
            msg.get("content") if isinstance(msg, dict) else ""
        )
        parts.append(f"{role}: {content}")
    return "\n".join(parts)


def estimate_response_cost_usd(
    *,
    provider: str,
    prompt_tokens: int,
    completion_tokens: int,
    settings: Settings,
) -> float:
    in_rate = settings.cost_per_1k_input.get(provider, 0.0)
    out_rate = settings.cost_per_1k_output.get(provider, 0.0)
    return (prompt_tokens / 1000.0) * in_rate + (completion_tokens / 1000.0) * out_rate


def build_semantic_cache(
    settings: Settings | None = None,
    *,
    client: redis.Redis | None = None,
    embedder: EmbeddingProvider | None = None,
    metrics: CacheMetrics | None = None,
) -> SemanticCache:
    cfg = settings or default_settings
    redis_client = client or redis.from_url(cfg.redis_url, decode_responses=False)
    emb = embedder or get_embedder(
        provider=cfg.cache_embedding_provider,
        dim=cfg.cache_embedding_dim,
    )
    return SemanticCache(
        redis_client,
        emb,
        enabled=cfg.cache_enabled,
        similarity_threshold=cfg.cache_similarity_threshold,
        ttl_seconds=cfg.cache_ttl_seconds,
        max_entries=cfg.cache_max_entries,
        metrics=metrics or cache_metrics,
    )


def get_semantic_cache(settings: Settings | None = None) -> SemanticCache:
    global _cache
    if settings is not None:
        return build_semantic_cache(settings)
    if _cache is None:
        _cache = build_semantic_cache(default_settings)
        logger.info(
            "semantic cache ready enabled=%s threshold=%.3f ttl=%ss max_entries=%s embedder=%s",
            default_settings.cache_enabled,
            default_settings.cache_similarity_threshold,
            default_settings.cache_ttl_seconds,
            default_settings.cache_max_entries,
            default_settings.cache_embedding_provider,
        )
    return _cache


async def reset_semantic_cache() -> None:
    """Close and clear the singleton (tests)."""
    global _cache
    if _cache is not None:
        try:
            await _cache.close()
        except Exception:  # noqa: BLE001
            pass
    _cache = None
