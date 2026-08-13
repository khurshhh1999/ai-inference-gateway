from __future__ import annotations

import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient

from app.cache.embeddings import HashingEmbedder, combined_similarity, cosine_similarity
from app.cache.metrics import CacheMetrics
from app.cache.semantic import SemanticCache, prompt_from_messages
from app.main import app
from app.models import ChatChoice, ChatChoiceMessage, ChatCompletionResponse, Usage


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_hashing_embedder_near_duplicates() -> None:
    emb = HashingEmbedder(dim=256)
    a_text = "Explain semantic caching in one sentence."
    b_text = "Explain semantic caching in a single sentence."
    c_text = "What is the capital of France?"
    a = emb.embed(a_text)
    b = emb.embed(b_text)
    c = emb.embed(c_text)
    assert cosine_similarity(a, a) == pytest.approx(1.0, abs=1e-6)
    assert combined_similarity(
        query_embedding=a,
        stored_embedding=b,
        query_prompt=a_text,
        stored_prompt=b_text,
    ) >= 0.90
    assert combined_similarity(
        query_embedding=a,
        stored_embedding=c,
        query_prompt=a_text,
        stored_prompt=c_text,
    ) < 0.85


def test_prompt_from_messages() -> None:
    text = prompt_from_messages(
        [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hello"},
        ]
    )
    assert "system: be brief" in text
    assert "user: hello" in text


@pytest.mark.asyncio
async def test_semantic_cache_hit_and_tenant_isolation() -> None:
    metrics = CacheMetrics()
    cache = SemanticCache(
        FakeAsyncRedis(decode_responses=False),
        HashingEmbedder(dim=256),
        similarity_threshold=0.90,
        ttl_seconds=60,
        max_entries=10,
        metrics=metrics,
        index_backend="scan",
    )
    response = ChatCompletionResponse(
        id="chatcmpl-1",
        created=1,
        model="mock-small",
        choices=[ChatChoice(message=ChatChoiceMessage(content="cached answer"))],
        usage=Usage(prompt_tokens=3, completion_tokens=2, total_tokens=5),
        provider="mock",
        cached=False,
        route_reason="failover",
    )
    prompt = "Explain semantic caching in one sentence."
    await cache.store(
        tenant="acme",
        model="mock-small",
        prompt=prompt,
        response=response,
        cost_usd=0.01,
    )

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
async def test_semantic_cache_max_entries_eviction() -> None:
    cache = SemanticCache(
        FakeAsyncRedis(decode_responses=False),
        HashingEmbedder(dim=256),
        similarity_threshold=0.99,
        ttl_seconds=60,
        max_entries=2,
        metrics=CacheMetrics(),
        index_backend="scan",
    )
    base = ChatCompletionResponse(
        id="chatcmpl-x",
        created=1,
        model="mock-small",
        choices=[ChatChoice(message=ChatChoiceMessage(content="x"))],
        usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        provider="mock",
    )
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


def test_chat_near_duplicate_cache_hit(client: TestClient) -> None:
    first = client.post(
        "/v1/chat/completions",
        headers={"X-Tenant-Id": "demo"},
        json={
            "model": "mock-small",
            "messages": [
                {"role": "user", "content": "Explain semantic caching in one sentence."}
            ],
        },
    )
    assert first.status_code == 200
    assert first.json()["cached"] is False

    second = client.post(
        "/v1/chat/completions",
        headers={"X-Tenant-Id": "demo"},
        json={
            "model": "mock-small",
            "messages": [
                {
                    "role": "user",
                    "content": "Explain semantic caching in a single sentence.",
                }
            ],
        },
    )
    assert second.status_code == 200
    body = second.json()
    assert body["cached"] is True
    assert body["route_reason"] == "cache_hit"
    assert body["choices"][0]["message"]["content"] == first.json()["choices"][0]["message"][
        "content"
    ]

    stats = client.get("/v1/cache/stats")
    assert stats.status_code == 200
    assert stats.json()["cache_hit_total"] >= 1
    assert stats.json()["estimated_usd_saved"] > 0
    assert stats.json()["index_backend"] == "scan"


def test_cache_bypass_header(client: TestClient) -> None:
    client.post(
        "/v1/chat/completions",
        headers={"X-Tenant-Id": "bypass"},
        json={
            "model": "mock-small",
            "messages": [{"role": "user", "content": "What is a circuit breaker?"}],
        },
    )
    bypassed = client.post(
        "/v1/chat/completions",
        headers={"X-Tenant-Id": "bypass", "X-Cache-Bypass": "1"},
        json={
            "model": "mock-small",
            "messages": [{"role": "user", "content": "What is a circuit breaker?"}],
        },
    )
    assert bypassed.status_code == 200
    assert bypassed.json()["cached"] is False


def test_health_includes_cache(client: TestClient) -> None:
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["cache"]["enabled"] is True
    assert body["cache"]["healthy"] is True
    assert body["cache"]["index_backend"] in {"scan", "auto", "redisearch"}
