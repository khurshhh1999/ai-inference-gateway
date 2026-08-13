from __future__ import annotations

import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient

from app.budget.meter import BudgetMeter, reset_budget_meter
from app.cache.embeddings import HashingEmbedder
from app.cache.metrics import cache_metrics
from app.cache.semantic import SemanticCache, reset_semantic_cache
from app.config import Settings
from app.main import app
from app.providers import reset_routing_engine


@pytest.fixture(autouse=True)
async def _reset_singletons(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_routing_engine()
    await reset_semantic_cache()
    await reset_budget_meter()
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
        index_backend="scan",
    )
    budget_settings = Settings(
        budget_enabled=True,
        budget_mock_usd=0.002,
        budget_usd_per_day=10.0,
        budget_tokens_per_day=1_000_000.0,
        budget_usd_per_minute=None,
        budget_tokens_per_minute=None,
        budget_usd_per_month=100.0,
        budget_tokens_per_month=10_000_000.0,
        budget_soft_ratio=0.8,
        budget_hard_status=402,
        tenant_budgets="",
        cache_enabled=True,
    )
    meter = BudgetMeter(client, budget_settings)

    monkeypatch.setattr("app.cache.semantic._cache", cache)
    monkeypatch.setattr("app.api.chat.get_semantic_cache", lambda: cache)
    monkeypatch.setattr("app.main.get_semantic_cache", lambda: cache)
    monkeypatch.setattr("app.budget.meter._meter", meter)
    monkeypatch.setattr("app.api.chat.get_budget_meter", lambda: meter)
    monkeypatch.setattr("app.main.get_budget_meter", lambda: meter)
    monkeypatch.setattr("app.api.chat.settings", budget_settings)

    yield

    reset_routing_engine()
    await reset_semantic_cache()
    await reset_budget_meter()
    cache_metrics.reset()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def budget_meter(monkeypatch: pytest.MonkeyPatch) -> BudgetMeter:
    """Access the injected meter (same Redis as cache)."""
    from app.budget.meter import get_budget_meter

    return get_budget_meter()
