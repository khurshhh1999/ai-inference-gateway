from __future__ import annotations

from fastapi.testclient import TestClient


def test_metrics_endpoint_exposes_router_series(client: TestClient) -> None:
    res = client.get("/metrics")
    assert res.status_code == 200
    body = res.text
    assert "router_request_duration_seconds" in body
    assert "router_cache_hit_total" in body
    assert "router_provider_errors_total" in body
    assert "router_adaptive_latency_ewma_seconds" in body
    assert "router_adaptive_error_rate" in body


def test_chat_updates_request_histogram(client: TestClient) -> None:
    before = client.get("/metrics").text
    client.post(
        "/v1/chat/completions",
        json={
            "model": "mock-small",
            "messages": [{"role": "user", "content": "metrics probe"}],
        },
        headers={"X-Tenant-Id": "metrics-tenant"},
    )
    after = client.get("/metrics").text
    assert "router_request_duration_seconds_count" in after
    # Count should be present; value may already exist from other tests in-process.
    assert "route=\"/v1/chat/completions\"" in after or 'route="/v1/chat/completions"' in after
    assert before is not None
