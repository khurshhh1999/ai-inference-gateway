from __future__ import annotations

from app.request_id import resolve_request_id


def test_resolve_request_id_accepts_safe_client_value() -> None:
    assert resolve_request_id("abc-123") == "abc-123"
    assert resolve_request_id("  demo:req_1  ") == "demo:req_1"


def test_resolve_request_id_rejects_unsafe_and_mints_uuid() -> None:
    minted = resolve_request_id("bad id with spaces")
    assert minted != "bad id with spaces"
    assert len(minted) == 36

    minted2 = resolve_request_id(None)
    assert len(minted2) == 36
