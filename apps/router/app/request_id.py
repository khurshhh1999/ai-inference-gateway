from __future__ import annotations

import re
import uuid

_SAFE = re.compile(r"^[A-Za-z0-9._\-:+]{1,128}$")
_MAX_LEN = 128


def resolve_request_id(raw: str | None) -> str:
    """Accept a client id when safe; otherwise mint a new UUID."""
    if raw is None:
        return str(uuid.uuid4())
    trimmed = raw.strip()
    if not trimmed or len(trimmed) > _MAX_LEN or not _SAFE.match(trimmed):
        return str(uuid.uuid4())
    return trimmed
