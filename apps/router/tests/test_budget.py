from __future__ import annotations

import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient

from app.budget.meter import BudgetExceededError, BudgetMeter
from app.budget.pricing import billable_cost_usd
from app.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        budget_enabled=True,
        budget_mock_usd=0.002,
        budget_usd_per_day=0.005,
        budget_tokens_per_day=1_000_000.0,
        budget_usd_per_minute=None,
        budget_tokens_per_minute=None,
        budget_usd_per_month=1.0,
        budget_tokens_per_month=10_000_000.0,
        budget_soft_ratio=0.8,
        budget_hard_status=402,
        tenant_budgets="tight:usd_day:0.003,tokens_day:10000",
        cache_enabled=False,
    )


@pytest.fixture
async def meter(settings: Settings) -> BudgetMeter:
    client = FakeAsyncRedis(decode_responses=False)
    return BudgetMeter(client, settings)


@pytest.mark.asyncio
async def test_record_and_usage_match(meter: BudgetMeter) -> None:
    await meter.record(
        tenant="acme",
        usd=0.002,
        tokens=42,
        provider="mock",
        model="mock-small",
    )
    status = await meter.usage("acme")
    day = next(w for w in status.windows if w.window == "day")
    assert day.usd_used == pytest.approx(0.002)
    assert day.tokens_used == 42
    assert day.usd_remaining == pytest.approx(0.003)


@pytest.mark.asyncio
async def test_hard_limit_rejects(meter: BudgetMeter) -> None:
    await meter.record(
        tenant="acme",
        usd=0.005,
        tokens=10,
        provider="mock",
        model="mock-small",
    )
    with pytest.raises(BudgetExceededError) as excinfo:
        await meter.check("acme", estimated_usd=0.001)
    assert excinfo.value.metric == "usd"
    assert excinfo.value.window == "day"
    assert excinfo.value.status_code == 402


@pytest.mark.asyncio
async def test_soft_warning_near_limit(meter: BudgetMeter) -> None:
    # soft_ratio 0.8 * 0.005 = 0.004
    await meter.record(
        tenant="acme",
        usd=0.004,
        tokens=10,
        provider="mock",
        model="mock-small",
    )
    check = await meter.check("acme")
    assert check.allowed is True
    assert check.soft_warning is True


@pytest.mark.asyncio
async def test_tenant_override_limits(meter: BudgetMeter) -> None:
    limits = meter.limits_for("tight")
    assert limits["usd_day"] == 0.003
    await meter.record(
        tenant="tight",
        usd=0.003,
        tokens=5,
        provider="mock",
        model="mock-small",
    )
    with pytest.raises(BudgetExceededError):
        await meter.check("tight", estimated_usd=0.0001)


def test_billable_cost_uses_mock_fallback() -> None:
    cfg = Settings(budget_mock_usd=0.002, provider_cost_per_1k_input="mock:0.0")
    cost = billable_cost_usd(
        provider="mock",
        model="mock-small",
        prompt_tokens=10,
        completion_tokens=20,
        settings=cfg,
    )
    assert cost == pytest.approx(0.002)


def test_billable_cost_model_override() -> None:
    cfg = Settings(
        model_cost_per_1k_input="mock-small:1.0",
        model_cost_per_1k_output="mock-small:2.0",
        budget_mock_usd=0.002,
    )
    cost = billable_cost_usd(
        provider="mock",
        model="mock-small",
        prompt_tokens=1000,
        completion_tokens=1000,
        settings=cfg,
    )
    assert cost == pytest.approx(3.0)


def test_http_rejects_over_budget(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.budget.meter import get_budget_meter
    from app.config import Settings

    tight = Settings(
        budget_enabled=True,
        budget_mock_usd=0.002,
        budget_usd_per_day=0.003,
        budget_tokens_per_day=1_000_000.0,
        budget_hard_status=402,
        budget_soft_ratio=0.8,
        cache_enabled=True,
    )
    meter = get_budget_meter()
    meter._settings = tight  # noqa: SLF001

    # First request meters 0.002; second should still fit (0.004 > 0.003) after first+check
    r1 = client.post(
        "/v1/chat/completions",
        headers={"X-Tenant-Id": "budget-http", "X-Cache-Bypass": "1"},
        json={
            "model": "mock-small",
            "messages": [{"role": "user", "content": "one"}],
        },
    )
    assert r1.status_code == 200

    r2 = client.post(
        "/v1/chat/completions",
        headers={"X-Tenant-Id": "budget-http", "X-Cache-Bypass": "1"},
        json={
            "model": "mock-small",
            "messages": [{"role": "user", "content": "two"}],
        },
    )
    # After one 0.002 spend, remaining 0.001; second estimated 0 still passes check
    # then records another 0.002 → used 0.004. Third should fail on check.
    assert r2.status_code == 200

    r3 = client.post(
        "/v1/chat/completions",
        headers={"X-Tenant-Id": "budget-http", "X-Cache-Bypass": "1"},
        json={
            "model": "mock-small",
            "messages": [{"role": "user", "content": "three"}],
        },
    )
    assert r3.status_code == 402
    detail = r3.json()["detail"]
    assert detail["error"] == "budget_exceeded"
    assert detail["tenant"] == "budget-http"


def test_usage_endpoint_matches_summed_costs(client: TestClient) -> None:
    tenant = "usage-sum"
    n = 3
    for i in range(n):
        res = client.post(
            "/v1/chat/completions",
            headers={"X-Tenant-Id": tenant, "X-Cache-Bypass": "1"},
            json={
                "model": "mock-small",
                "messages": [{"role": "user", "content": f"msg {i}"}],
            },
        )
        assert res.status_code == 200

    usage = client.get(f"/v1/tenants/{tenant}/usage")
    assert usage.status_code == 200
    body = usage.json()
    day = next(w for w in body["windows"] if w["window"] == "day")
    assert day["usd_used"] == pytest.approx(0.002 * n)
    assert day["tokens_used"] > 0

    budget = client.get(f"/v1/tenants/{tenant}/budget")
    assert budget.status_code == 200
    assert budget.json()["tenant"] == tenant
    assert budget.json()["enabled"] is True


def test_cache_hit_does_not_meter(client: TestClient) -> None:
    tenant = "cache-no-meter"
    payload = {
        "model": "mock-small",
        "messages": [{"role": "user", "content": "identical prompt for cache"}],
    }
    r1 = client.post(
        "/v1/chat/completions",
        headers={"X-Tenant-Id": tenant},
        json=payload,
    )
    assert r1.status_code == 200
    assert r1.json()["cached"] is False

    usage_after_miss = client.get(f"/v1/tenants/{tenant}/usage").json()
    day_miss = next(w for w in usage_after_miss["windows"] if w["window"] == "day")
    usd_after_miss = day_miss["usd_used"]
    tokens_after_miss = day_miss["tokens_used"]
    assert usd_after_miss == pytest.approx(0.002)

    r2 = client.post(
        "/v1/chat/completions",
        headers={"X-Tenant-Id": tenant},
        json=payload,
    )
    assert r2.status_code == 200
    assert r2.json()["cached"] is True

    usage_after_hit = client.get(f"/v1/tenants/{tenant}/usage").json()
    day_hit = next(w for w in usage_after_hit["windows"] if w["window"] == "day")
    assert day_hit["usd_used"] == pytest.approx(usd_after_miss)
    assert day_hit["tokens_used"] == tokens_after_miss


def test_health_includes_budget(client: TestClient) -> None:
    res = client.get("/health")
    assert res.status_code == 200
    assert "budget" in res.json()
    assert res.json()["budget"]["enabled"] is True
