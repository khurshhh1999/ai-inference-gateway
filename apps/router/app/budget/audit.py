"""Structured audit log for spend events."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("app.budget.audit")


def log_spend_event(
    *,
    tenant: str,
    usd: float,
    tokens: int,
    provider: str,
    model: str,
    soft_warning: bool = False,
    extra: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "event": "spend_audit",
        "tenant": tenant,
        "usd": round(usd, 8),
        "tokens": tokens,
        "provider": provider,
        "model": model,
        "soft_warning": soft_warning,
    }
    if extra:
        payload.update(extra)
    logger.info("spend_audit %s", payload)


def log_budget_rejection(
    *,
    tenant: str,
    window: str,
    metric: str,
    used: float,
    limit: float,
    hard_status: int,
) -> None:
    logger.warning(
        "budget_exceeded %s",
        {
            "event": "budget_exceeded",
            "tenant": tenant,
            "window": window,
            "metric": metric,
            "used": used,
            "limit": limit,
            "hard_status": hard_status,
        },
    )
