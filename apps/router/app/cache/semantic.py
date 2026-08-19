from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

import redis.asyncio as redis

from app.cache.embeddings import (
    EmbeddingProvider,
    combined_similarity,
    get_embedder,
    sequence_similarity,
)
from app.cache.index_backend import (
    AUTO_BACKEND,
    CacheIndexBackend,
    resolve_index_backend,
)
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
    """Tenant-scoped semantic response cache.

    Default lookup is an O(n) scan of the namespace (vanilla Redis). When Redis
    Query Engine / RediSearch is available, ``CACHE_INDEX_BACKEND=auto`` (or
    ``redisearch``) switches to HNSW KNN and re-ranks the top-k with the same
    combined similarity used by the scan path.
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
        index_backend: str = AUTO_BACKEND,
        ann_top_k: int = 25,
        ann_ef_runtime: int = 64,
    ) -> None:
        self._redis = client
        self._embedder = embedder
        self.enabled = enabled
        self.similarity_threshold = similarity_threshold
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._metrics = metrics or cache_metrics
        self._requested_backend = index_backend
        self.ann_top_k = max(1, ann_top_k)
        self.ann_ef_runtime = max(1, ann_ef_runtime)
        self._index: CacheIndexBackend | None = None

    @property
    def embedder(self) -> EmbeddingProvider:
        return self._embedder

    def embed(self, text: str) -> list[float]:
        """Same vectors as semantic-cache lookup (tenant-agnostic)."""
        return self._embedder.embed(text)

    @property
    def embedding_provider_name(self) -> str:
        return type(self._embedder).__name__

    @property
    def embedding_dim(self) -> int:
        return self._embedder.dim

    @property
    def index_backend_name(self) -> str:
        if self._index is None:
            return self._requested_backend
        return self._index.name

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

    async def _ensure_backend(self) -> CacheIndexBackend:
        if self._index is None:
            self._index = await resolve_index_backend(
                self._redis,
                self._requested_backend,
                dim=self._embedder.dim,
                ann_top_k=self.ann_top_k,
                ann_ef_runtime=self.ann_ef_runtime,
            )
        return self._index

    def _score_candidate(
        self,
        *,
        query_embedding: list[float],
        query_prompt: str,
        stored_prompt: str,
        stored_embedding: list[float] | None,
        cosine: float | None,
    ) -> float:
        if stored_embedding is not None:
            return combined_similarity(
                query_embedding=query_embedding,
                stored_embedding=stored_embedding,
                query_prompt=query_prompt,
                stored_prompt=stored_prompt,
            )
        if cosine is not None:
            return max(cosine, sequence_similarity(query_prompt, stored_prompt))
        raise ValueError("candidate missing embedding and cosine")

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
        backend = await self._ensure_backend()
        found = await backend.candidates(
            ns,
            query,
            top_k=self.ann_top_k,
            ef_runtime=self.ann_ef_runtime,
        )

        best: CacheHit | None = None
        stale: list[str] = []

        for cand in found:
            try:
                score = self._score_candidate(
                    query_embedding=query,
                    query_prompt=prompt,
                    stored_prompt=cand.prompt,
                    stored_embedding=cand.embedding,
                    cosine=cand.cosine,
                )
            except ValueError:
                stale.append(cand.entry_id)
                continue
            if score < self.similarity_threshold:
                continue
            if best is None or score > best.similarity:
                response = ChatCompletionResponse.model_validate(cand.response)
                response.cached = True
                response.route_reason = "cache_hit"
                best = CacheHit(
                    response=response,
                    similarity=score,
                    saved_usd=float(cand.cost_usd),
                    entry_id=cand.entry_id,
                )

        if stale:
            await backend.drop_ids(ns, stale)

        if best is None:
            self._metrics.record_miss()
            logger.info(
                "cache miss tenant=%s model=%s backend=%s candidates=%s",
                tenant,
                model,
                backend.name,
                len(found),
            )
            return None

        self._metrics.record_hit(best.saved_usd)
        logger.info(
            "cache hit tenant=%s model=%s backend=%s similarity=%.4f saved_usd=%.6f entry=%s",
            tenant,
            model,
            backend.name,
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
        backend = await self._ensure_backend()
        await backend.upsert(ns, entry_id, embedding, payload, self.ttl_seconds)
        await backend.evict_if_needed(ns, self.max_entries)
        logger.info(
            "cache store tenant=%s model=%s backend=%s entry=%s cost_usd=%.6f",
            tenant,
            model,
            backend.name,
            entry_id,
            cost_usd,
        )
        return entry_id

    async def ping(self) -> bool:
        try:
            await self._ensure_backend()
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
        if hasattr(msg, "text"):
            content = msg.text()
        else:
            content = getattr(msg, "content", None) or (
                msg.get("content") if isinstance(msg, dict) else ""
            )
            if content is None:
                content = ""
            calls = getattr(msg, "tool_calls", None)
            if calls is None and isinstance(msg, dict):
                calls = msg.get("tool_calls")
            if calls:
                content = f"{content} {calls}".strip()
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
        index_backend=cfg.cache_index_backend,
        ann_top_k=cfg.cache_ann_top_k,
        ann_ef_runtime=cfg.cache_ann_ef_runtime,
    )


def get_semantic_cache(settings: Settings | None = None) -> SemanticCache:
    global _cache
    if settings is not None:
        return build_semantic_cache(settings)
    if _cache is None:
        _cache = build_semantic_cache(default_settings)
        logger.info(
            "semantic cache ready enabled=%s threshold=%.3f ttl=%ss max_entries=%s "
            "embedder=%s index_backend=%s ann_top_k=%s",
            default_settings.cache_enabled,
            default_settings.cache_similarity_threshold,
            default_settings.cache_ttl_seconds,
            default_settings.cache_max_entries,
            default_settings.cache_embedding_provider,
            default_settings.cache_index_backend,
            default_settings.cache_ann_top_k,
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
