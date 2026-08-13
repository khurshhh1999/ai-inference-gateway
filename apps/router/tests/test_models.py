from __future__ import annotations

from fastapi.testclient import TestClient

from app.catalog import EMBEDDING_MODEL_IDS, get_model, is_embedding_model, list_models
from app.config import Settings


def test_list_models(client: TestClient) -> None:
    res = client.get("/v1/models", headers={"X-Request-Id": "models-list-001"})
    assert res.status_code == 200
    assert res.headers.get("x-request-id") == "models-list-001"
    body = res.json()
    assert body["object"] == "list"
    ids = {item["id"] for item in body["data"]}
    assert "mock-small" in ids
    assert "gpt-proxy" in ids
    assert "text-embedding-3-small" in ids
    assert "text-embedding-hashing" in ids
    purposes = {item["id"]: item["purpose"] for item in body["data"]}
    assert purposes["mock-small"] == "chat"
    assert purposes["text-embedding-3-small"] == "embeddings"
    for item in body["data"]:
        assert item["object"] == "model"
        assert "owned_by" in item
        assert item["created"] == 1_700_000_000


def test_retrieve_model(client: TestClient) -> None:
    res = client.get("/v1/models/mock-small")
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == "mock-small"
    assert body["purpose"] == "chat"


def test_retrieve_unknown_model(client: TestClient) -> None:
    res = client.get("/v1/models/not-a-real-model")
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "model_not_found"


def test_catalog_helpers() -> None:
    settings = Settings(model_map="only-me=mock:only-me")
    ids = [c.id for c in list_models(settings)]
    assert ids[0] == "only-me"
    assert set(ids[1:]) == set(EMBEDDING_MODEL_IDS)
    assert get_model("only-me", settings) is not None
    assert get_model("missing", settings) is None
    assert is_embedding_model("text-embedding-3-small")
    assert not is_embedding_model("mock-small")
