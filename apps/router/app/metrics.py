"""Prometheus metrics for the router (stable `router_*` names)."""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

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
    "Router HTTP request latency",
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

CACHE_LOOKUP_CANDIDATES = Histogram(
    "router_cache_lookup_candidates",
    "Candidates scored per semantic cache lookup (scan ≈ namespace size; ANN ≈ top-k)",
    labelnames=("backend",),
    buckets=(1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500),
)

CACHE_INDEX_BACKEND = Gauge(
    "router_cache_index_backend_info",
    "Active semantic cache index backend (1 = in use)",
    labelnames=("backend",),
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

ADAPTIVE_LATENCY_EWMA = Gauge(
    "router_adaptive_latency_ewma_seconds",
    "Live EWMA provider latency from recent attempts",
    labelnames=("provider",),
)

ADAPTIVE_ERROR_RATE = Gauge(
    "router_adaptive_error_rate",
    "Live EWMA provider error rate (0-1)",
    labelnames=("provider",),
)

ADAPTIVE_SCORE = Gauge(
    "router_adaptive_score_seconds",
    "Adaptive routing score (lower is preferred): EWMA latency + error penalty",
    labelnames=("provider",),
)

ADAPTIVE_SAMPLES = Gauge(
    "router_adaptive_samples",
    "Live observations contributing to adaptive EWMA (0 when cold/stale)",
    labelnames=("provider",),
)

HEDGE_FIRED = Counter(
    "router_hedge_fired_total",
    "Hedge requests launched (secondary started while primary still in-flight)",
    labelnames=("primary", "secondary"),
)

HEDGE_WON = Counter(
    "router_hedge_won_total",
    "Races where a hedge was in-flight and this role produced the kept response",
    labelnames=("provider", "role"),
)

HEDGE_CANCELLED = Counter(
    "router_hedge_cancelled_total",
    "In-flight provider calls cancelled after the other racer succeeded",
    labelnames=("provider",),
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


def observe_cache_lookup_candidates(backend: str, count: int) -> None:
    CACHE_LOOKUP_CANDIDATES.labels(backend=backend[:32]).observe(max(0, count))


def set_cache_index_backend(backend: str) -> None:
    for name in ("scan", "redisearch"):
        CACHE_INDEX_BACKEND.labels(backend=name).set(1.0 if name == backend else 0.0)


def record_spend(provider: str, usd: float) -> None:
    if usd > 0:
        SPEND_USD.labels(provider=provider).inc(usd)


def record_budget_rejection(window: str, metric: str) -> None:
    BUDGET_REJECTIONS.labels(window=window, metric=metric).inc()


def record_route_decision(provider: str, reason: str) -> None:
    ROUTE_DECISIONS.labels(provider=provider, reason=reason[:64]).inc()


def record_hedge_fired(primary: str, secondary: str) -> None:
    HEDGE_FIRED.labels(primary=primary[:32], secondary=secondary[:32]).inc()


def record_hedge_won(provider: str, role: str) -> None:
    HEDGE_WON.labels(provider=provider[:32], role=role[:16]).inc()


def record_hedge_cancelled(provider: str) -> None:
    HEDGE_CANCELLED.labels(provider=provider[:32]).inc()


def set_adaptive_gauges(
    provider: str,
    *,
    latency_ms: float,
    error_rate: float,
    score_ms: float,
    samples: int,
) -> None:
    ADAPTIVE_LATENCY_EWMA.labels(provider=provider).set(max(0.0, latency_ms) / 1000.0)
    ADAPTIVE_ERROR_RATE.labels(provider=provider).set(min(1.0, max(0.0, error_rate)))
    ADAPTIVE_SCORE.labels(provider=provider).set(max(0.0, score_ms) / 1000.0)
    ADAPTIVE_SAMPLES.labels(provider=provider).set(max(0, samples))


def render_latest() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
