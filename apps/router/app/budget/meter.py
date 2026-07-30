"""Redis-backed per-tenant USD / token budgets with soft + hard limits."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as redis

from app.budget.audit import log_budget_rejection, log_spend_event
from app.config import Settings, settings as default_settings

logger = logging.getLogger(__name__)

_meter: BudgetMeter | None = None

_WINDOWS = ("minute", "day", "month")


class BudgetExceededError(Exception):
    """Hard budget limit reached for a tenant window."""

    def __init__(
        self,
        *,
        tenant: str,
        window: str,
        metric: str,
        used: float,
        limit: float,
        status_code: int,
    ) -> None:
        self.tenant = tenant
        self.window = window
        self.metric = metric
        self.used = used
        self.limit = limit
        self.status_code = status_code
        super().__init__(
            f"Tenant '{tenant}' exceeded {metric} {window} budget "
            f"({used:.6g} / {limit:.6g})"
        )


@dataclass(frozen=True)
class WindowUsage:
    window: str
    key_suffix: str
    usd_used: float
    tokens_used: int
    usd_limit: float | None
    tokens_limit: float | None
    usd_remaining: float | None
    tokens_remaining: float | None
    soft_warning: bool


@dataclass(frozen=True)
class BudgetStatus:
    tenant: str
    enabled: bool
    soft_ratio: float
    hard_status: int
    windows: list[WindowUsage]
    soft_warning: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "tenant": self.tenant,
            "enabled": self.enabled,
            "soft_ratio": self.soft_ratio,
            "hard_status": self.hard_status,
            "soft_warning": self.soft_warning,
            "windows": [
                {
                    "window": w.window,
                    "period": w.key_suffix,
                    "usd_used": w.usd_used,
                    "tokens_used": w.tokens_used,
                    "usd_limit": w.usd_limit,
                    "tokens_limit": w.tokens_limit,
                    "usd_remaining": w.usd_remaining,
                    "tokens_remaining": w.tokens_remaining,
                    "soft_warning": w.soft_warning,
                }
                for w in self.windows
            ],
        }


@dataclass(frozen=True)
class CheckResult:
    allowed: bool
    soft_warning: bool
    status: BudgetStatus


class BudgetMeter:
    """Check and record per-tenant spend against Redis counters."""

    def __init__(
        self,
        client: redis.Redis,
        settings: Settings,
    ) -> None:
        self._redis = client
        self._settings = settings

    @property
    def enabled(self) -> bool:
        return self._settings.budget_enabled

    @property
    def hard_status(self) -> int:
        return self._settings.budget_hard_status

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:  # noqa: BLE001
            return False

    async def close(self) -> None:
        try:
            await self._redis.aclose()
        except Exception:  # noqa: BLE001
            pass

    async def check(self, tenant: str, *, estimated_usd: float = 0.0, estimated_tokens: int = 0) -> CheckResult:
        """Reject when any hard window would be exceeded; soft-warn near limits."""
        status = await self.usage(tenant)
        if not self.enabled:
            return CheckResult(allowed=True, soft_warning=False, status=status)

        for window in status.windows:
            if window.usd_limit is not None:
                projected = window.usd_used + max(0.0, estimated_usd)
                exhausted = window.usd_used >= window.usd_limit - 1e-12
                would_exceed = projected > window.usd_limit + 1e-12
                if exhausted or would_exceed:
                    exc = BudgetExceededError(
                        tenant=tenant,
                        window=window.window,
                        metric="usd",
                        used=window.usd_used,
                        limit=window.usd_limit,
                        status_code=self.hard_status,
                    )
                    log_budget_rejection(
                        tenant=tenant,
                        window=window.window,
                        metric="usd",
                        used=window.usd_used,
                        limit=window.usd_limit,
                        hard_status=self.hard_status,
                    )
                    raise exc
            if window.tokens_limit is not None:
                projected = window.tokens_used + max(0, estimated_tokens)
                exhausted = window.tokens_used >= window.tokens_limit
                would_exceed = projected > window.tokens_limit
                if exhausted or would_exceed:
                    exc = BudgetExceededError(
                        tenant=tenant,
                        window=window.window,
                        metric="tokens",
                        used=float(window.tokens_used),
                        limit=window.tokens_limit,
                        status_code=self.hard_status,
                    )
                    log_budget_rejection(
                        tenant=tenant,
                        window=window.window,
                        metric="tokens",
                        used=float(window.tokens_used),
                        limit=window.tokens_limit,
                        hard_status=self.hard_status,
                    )
                    raise exc

        return CheckResult(
            allowed=True,
            soft_warning=status.soft_warning,
            status=status,
        )

    async def record(
        self,
        *,
        tenant: str,
        usd: float,
        tokens: int,
        provider: str,
        model: str,
    ) -> BudgetStatus:
        """Increment Redis counters after a successful (non-cache) completion."""
        if not self.enabled or (usd <= 0.0 and tokens <= 0):
            return await self.usage(tenant)

        now = datetime.now(UTC)
        pipe = self._redis.pipeline()
        for window in _WINDOWS:
            suffix, ttl = _window_key_and_ttl(window, now)
            usd_key = _counter_key(tenant, "usd", window, suffix)
            tok_key = _counter_key(tenant, "tok", window, suffix)
            pipe.incrbyfloat(usd_key, float(usd))
            pipe.expire(usd_key, ttl)
            pipe.incrby(tok_key, int(tokens))
            pipe.expire(tok_key, ttl)
        await pipe.execute()

        status = await self.usage(tenant)
        log_spend_event(
            tenant=tenant,
            usd=usd,
            tokens=tokens,
            provider=provider,
            model=model,
            soft_warning=status.soft_warning,
            extra={"windows": [w.window for w in status.windows if w.soft_warning]},
        )
        if status.soft_warning:
            logger.warning(
                "budget soft threshold reached tenant=%s soft_ratio=%.2f",
                tenant,
                self._settings.budget_soft_ratio,
            )
        return status

    async def usage(self, tenant: str) -> BudgetStatus:
        limits = self._settings.budget_for_tenant(tenant)
        soft_ratio = self._settings.budget_soft_ratio
        now = datetime.now(UTC)
        windows: list[WindowUsage] = []
        any_soft = False

        for window in _WINDOWS:
            suffix, _ttl = _window_key_and_ttl(window, now)
            usd_raw = await self._redis.get(_counter_key(tenant, "usd", window, suffix))
            tok_raw = await self._redis.get(_counter_key(tenant, "tok", window, suffix))
            usd_used = float(usd_raw) if usd_raw is not None else 0.0
            tokens_used = int(float(tok_raw)) if tok_raw is not None else 0

            usd_limit = limits.get(f"usd_{window}")
            tokens_limit = limits.get(f"tokens_{window}")

            usd_remaining = None if usd_limit is None else max(0.0, usd_limit - usd_used)
            tokens_remaining = (
                None if tokens_limit is None else max(0.0, tokens_limit - float(tokens_used))
            )

            soft = False
            if usd_limit is not None and usd_limit > 0 and usd_used >= soft_ratio * usd_limit:
                soft = True
            if (
                tokens_limit is not None
                and tokens_limit > 0
                and tokens_used >= soft_ratio * tokens_limit
            ):
                soft = True
            any_soft = any_soft or soft

            windows.append(
                WindowUsage(
                    window=window,
                    key_suffix=suffix,
                    usd_used=usd_used,
                    tokens_used=tokens_used,
                    usd_limit=usd_limit,
                    tokens_limit=tokens_limit,
                    usd_remaining=usd_remaining,
                    tokens_remaining=tokens_remaining,
                    soft_warning=soft,
                )
            )

        return BudgetStatus(
            tenant=tenant,
            enabled=self.enabled,
            soft_ratio=soft_ratio,
            hard_status=self.hard_status,
            windows=windows,
            soft_warning=any_soft,
        )

    def limits_for(self, tenant: str) -> dict[str, float | None]:
        return self._settings.budget_for_tenant(tenant)


def _counter_key(tenant: str, metric: str, window: str, suffix: str) -> str:
    # metric: usd | tok; window: minute | day | month
    short = {"minute": "m", "day": "d", "month": "M"}[window]
    return f"budget:{tenant}:{metric}:{short}:{suffix}"


def _window_key_and_ttl(window: str, now: datetime) -> tuple[str, int]:
    if window == "minute":
        return now.strftime("%Y%m%d%H%M"), 120
    if window == "day":
        return now.strftime("%Y%m%d"), 86_400 * 2
    if window == "month":
        return now.strftime("%Y%m"), 86_400 * 40
    raise ValueError(f"unknown window: {window}")


def build_budget_meter(
    settings: Settings | None = None,
    *,
    client: redis.Redis | None = None,
) -> BudgetMeter:
    cfg = settings or default_settings
    redis_client = client or redis.from_url(cfg.redis_url, decode_responses=False)
    return BudgetMeter(redis_client, cfg)


def get_budget_meter(settings: Settings | None = None) -> BudgetMeter:
    global _meter
    if settings is not None:
        return build_budget_meter(settings)
    if _meter is None:
        _meter = build_budget_meter(default_settings)
        logger.info(
            "budget meter ready enabled=%s soft_ratio=%.2f hard_status=%s",
            default_settings.budget_enabled,
            default_settings.budget_soft_ratio,
            default_settings.budget_hard_status,
        )
    return _meter


async def reset_budget_meter() -> None:
    """Close and clear the singleton (tests)."""
    global _meter
    if _meter is not None:
        try:
            await _meter.close()
        except Exception:  # noqa: BLE001
            pass
    _meter = None
