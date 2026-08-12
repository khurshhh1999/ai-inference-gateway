from __future__ import annotations

from app.config import Settings
from app.models import ChatCompletionRequest
from app.providers.base import Provider
from app.routing.signals import AdaptiveSignals

VALID_POLICIES = frozenset(
    {"prefer_cost", "prefer_latency", "prefer_provider", "failover", "adaptive"}
)


def _rough_cost_usd(
    provider_name: str,
    request: ChatCompletionRequest,
    settings: Settings,
) -> float:
    """Policy-time cost hint from configured rates (not a billable meter)."""
    prompt_tokens = max(1, sum(len(m.content.split()) for m in request.messages))
    completion_tokens = request.max_tokens or 64
    in_rate = settings.cost_per_1k_input.get(provider_name, 0.0)
    out_rate = settings.cost_per_1k_output.get(provider_name, 0.0)
    return (prompt_tokens / 1000.0) * in_rate + (completion_tokens / 1000.0) * out_rate


def ordered_candidates(
    request: ChatCompletionRequest,
    providers: dict[str, Provider],
    settings: Settings,
    *,
    policy: str | None = None,
    signals: AdaptiveSignals | None = None,
) -> tuple[list[Provider], str]:
    """Return providers in try-order plus a human-readable reason code."""
    policy_name = (policy or settings.routing_policy).lower().strip()
    if policy_name not in VALID_POLICIES:
        policy_name = "failover"

    available = list(providers.values())
    if not available:
        return [], "no_providers"

    if policy_name == "prefer_cost":
        ranked = sorted(
            available,
            key=lambda p: _rough_cost_usd(p.name, request, settings),
        )
        return ranked, "cost"

    if policy_name == "prefer_latency":
        ranked = sorted(
            available,
            key=lambda p: settings.latency_hints_ms.get(p.name, 1_000.0),
        )
        return ranked, "latency"

    if policy_name == "adaptive":
        tracker = signals or AdaptiveSignals(
            alpha=settings.adaptive_ewma_alpha,
            error_penalty_ms=settings.adaptive_error_penalty_ms,
            min_samples=settings.adaptive_min_samples,
            stale_after_seconds=settings.adaptive_stale_after_seconds,
            latency_hints_ms=settings.latency_hints_ms,
        )
        # Stale providers are probed first so a recovered endpoint can earn traffic
        # again without a config change; everyone else ranks on live score.
        stale = [p for p in available if tracker.is_stale(p.name)]
        rest = [p for p in available if not tracker.is_stale(p.name)]
        ranked = stale + sorted(rest, key=lambda p: tracker.score(p.name))
        return ranked, "adaptive"

    # prefer_provider + failover: primary first, then configured fallbacks, then rest
    ordered_names: list[str] = []
    primary = settings.routing_primary.strip()
    if primary:
        ordered_names.append(primary)
    for name in settings.fallback_providers:
        if name not in ordered_names:
            ordered_names.append(name)
    for name in providers:
        if name not in ordered_names:
            ordered_names.append(name)

    ranked = [providers[n] for n in ordered_names if n in providers]
    reason = "affinity" if policy_name == "prefer_provider" else "failover"
    return ranked, reason
