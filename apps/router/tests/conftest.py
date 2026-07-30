from __future__ import annotations

import pytest
from fakeredis import FakeAsyncRedis

from app.cache.embeddings import HashingEmbedder
from app.cache.metrics import cache_metrics
from app.cache.semantic import SemanticCache, reset_semantic_cache
from app.providers import reset_routing_engine


@pytest.fixture(autouse=True)
async def _reset_singletons(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_routing_engine()
    await reset_semantic_cache()
    cache_metrics.reset()

    client = FakeAsyncRedis(decode_responses=False)
    cache = SemanticCache(
        client,
        HashingEmbedder(dim=256),
        enabled=True,
        similarity_threshold=0.90,
        ttl_seconds=3600,
        max_entries=100,
        metrics=cache_metrics,
    )
    monkeypatch.setattr("app.cache.semantic._cache", cache)
    monkeypatch.setattr("app.api.chat.get_semantic_cache", lambda: cache)
    monkeypatch.setattr("app.main.get_semantic_cache", lambda: cache)

    yield

    reset_routing_engine()
    await reset_semantic_cache()
    cache_metrics.reset()
