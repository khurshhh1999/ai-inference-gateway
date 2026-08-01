"""Prometheus metrics for the router (stable `router_*` names)."""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

# Explicit buckets so p95 claims are reproducible from /metrics.
_LATENCY_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.075,
    0.1,
    0.15,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

REQUEST_DURATION = Histogram(
    "router_request_duration_seconds",
    "Chat completion request latency at the router",
    labelnames=("method", "route", "status", "cached", "stream"),
    buckets=_LATENCY_BUCKETS,
)

PROVIDER_DURATION = Histogram(
    "router_provider_duration_seconds",
    "Provider call latency (complete or open_stream first-byte)",
    labelnames=("provider", "outcome"),
    buckets=_LATENCY_BUCKETS,
)

PROVIDER_ERRORS = Counter(
    "router_provider_errors_total",
    "Provider failures observed during routing",
    labelnames=("provider", "error"),
)

CACHE_HITS = Counter(
    "router_cache_hit_total",
    "Semantic cache hits",
)

CACHE_MISSES = Counter(
    "router_cache_miss_total",
    "Semantic cache misses",
)

USD_SAVED = Counter(
    "router_estimated_usd_saved_total",
    "Estimated USD avoided via semantic cache hits",
)

SPEND_USD = Counter(
    "router_spend_usd_total",
    "Billable USD recorded after successful completions",
    labelnames=("provider",),
)

BUDGET_REJECTIONS = Counter(
    "router_budget_rejections_total",
    "Hard budget rejections before provider call",
    labelnames=("window", "metric"),
)

ROUTE_DECISIONS = Counter(
    "router_route_decisions_total",
    "Successful route selections",
    labelnames=("provider", "reason"),
)


def observe_request(
    *,
    method: str,
    route: str,
    status: int,
    cached: bool,
    stream: bool,
    seconds: float,
) -> None:
    REQUEST_DURATION.labels(
        method=method,
        route=route,
        status=str(status),
        cached="true" if cached else "false",
        stream="true" if stream else "false",
    ).observe(seconds)


def observe_provider(
    *,
    provider: str,
    outcome: str,
    seconds: float,
) -> None:
    PROVIDER_DURATION.labels(provider=provider, outcome=outcome).observe(seconds)


def record_provider_error(provider: str, error: str) -> None:
    # Keep label cardinality bounded.
    short = error.split(":", 1)[0][:64]
    PROVIDER_ERRORS.labels(provider=provider, error=short).inc()


def record_cache_hit(saved_usd: float) -> None:
    CACHE_HITS.inc()
    if saved_usd > 0:
        USD_SAVED.inc(saved_usd)


def record_cache_miss() -> None:
    CACHE_MISSES.inc()


def record_spend(provider: str, usd: float) -> None:
    if usd > 0:
        SPEND_USD.labels(provider=provider).inc(usd)


def record_budget_rejection(window: str, metric: str) -> None:
    BUDGET_REJECTIONS.labels(window=window, metric=metric).inc()


def record_route_decision(provider: str, reason: str) -> None:
    ROUTE_DECISIONS.labels(provider=provider, reason=reason[:64]).inc()


def render_latest() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
