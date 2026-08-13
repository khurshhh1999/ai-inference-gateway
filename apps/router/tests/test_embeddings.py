from __future__ import annotations

from fastapi.testclient import TestClient

from app.budget.meter import BudgetMeter
from app.cache.embeddings import HashingEmbedder, cosine_similarity, resize_embedding
from app.config import Settings


def test_embeddings_single_string(client: TestClient) -> None:
    res = client.post(
        "/v1/embeddings",
        json={"model": "text-embedding-3-small", "input": "semantic cache demo"},
        headers={"X-Request-Id": "emb-req-001", "X-Tenant-Id": "default"},
    )
    assert res.status_code == 200
    assert res.headers.get("x-request-id") == "emb-req-001"
    body = res.json()
    assert body["object"] == "list"
    assert body["model"] == "text-embedding-3-small"
    assert body["embedding_provider"] == "hashing"
    assert len(body["data"]) == 1
    vec = body["data"][0]["embedding"]
    assert body["data"][0]["index"] == 0
    assert body["data"][0]["object"] == "embedding"
    assert len(vec) == body["dim"] == 256
    norm = sum(x * x for x in vec) ** 0.5
    assert abs(norm - 1.0) < 1e-6
    assert body["usage"]["prompt_tokens"] >= 1
    assert body["usage"]["total_tokens"] == body["usage"]["prompt_tokens"]


def test_embeddings_batch_and_hashing_alias(client: TestClient) -> None:
    res = client.post(
        "/v1/embeddings",
        json={
            "model": "text-embedding-hashing",
            "input": ["alpha", "beta"],
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert [item["index"] for item in body["data"]] == [0, 1]
    a = body["data"][0]["embedding"]
    b = body["data"][1]["embedding"]
    assert cosine_similarity(a, b) < 0.999


def test_embeddings_dimensions(client: TestClient) -> None:
    res = client.post(
        "/v1/embeddings",
        json={
            "model": "text-embedding-3-small",
            "input": "dim shrink",
            "dimensions": 64,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["dim"] == 64
    assert len(body["data"][0]["embedding"]) == 64


def test_embeddings_rejects_chat_model(client: TestClient) -> None:
    res = client.post(
        "/v1/embeddings",
        json={"model": "mock-small", "input": "nope"},
    )
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "invalid_model"


def test_embeddings_validation(client: TestClient) -> None:
    res = client.post("/v1/embeddings", json={"model": "text-embedding-3-small", "input": []})
    assert res.status_code == 422


def test_embeddings_meters_tokens(client: TestClient) -> None:
    tenant = "emb-meter"
    before = client.get(f"/v1/tenants/{tenant}/usage").json()
    day_before = next(w for w in before["windows"] if w["window"] == "day")
    res = client.post(
        "/v1/embeddings",
        json={"model": "text-embedding-3-small", "input": "one two three four"},
        headers={"X-Tenant-Id": tenant},
    )
    assert res.status_code == 200
    billed = res.json()["usage"]["total_tokens"]
    after = client.get(f"/v1/tenants/{tenant}/usage").json()
    day_after = next(w for w in after["windows"] if w["window"] == "day")
    assert day_after["tokens_used"] == day_before["tokens_used"] + billed


def test_embeddings_hard_budget(client: TestClient, budget_meter: BudgetMeter) -> None:
    tight = Settings(
        budget_enabled=True,
        budget_mock_usd=0.002,
        budget_usd_per_day=None,
        budget_tokens_per_day=1,
        budget_usd_per_minute=None,
        budget_tokens_per_minute=None,
        budget_usd_per_month=None,
        budget_tokens_per_month=None,
        budget_soft_ratio=0.8,
        budget_hard_status=402,
        tenant_budgets="",
        cache_enabled=True,
    )
    budget_meter._settings = tight
    res = client.post(
        "/v1/embeddings",
        json={
            "model": "text-embedding-3-small",
            "input": "this prompt has more than one token",
        },
        headers={"X-Tenant-Id": "broke"},
    )
    assert res.status_code == 402
    assert res.json()["detail"]["error"] == "budget_exceeded"


def test_resize_embedding_roundtrip() -> None:
    vec = HashingEmbedder(dim=256).embed("hello")
    small = resize_embedding(vec, 64)
    assert len(small) == 64
    assert abs(sum(x * x for x in small) ** 0.5 - 1.0) < 1e-6
    wide = resize_embedding(vec, 320)
    assert len(wide) == 320
