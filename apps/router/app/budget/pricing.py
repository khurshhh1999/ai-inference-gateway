"""Billable cost from usage + config-driven pricing tables."""

from __future__ import annotations

from app.cache.semantic import estimate_response_cost_usd
from app.config import Settings


def billable_cost_usd(
    *,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    settings: Settings,
) -> float:
    """USD charged against the tenant budget for a completed call.

    Prefers per-model rates when configured, otherwise provider rates.
    Mock list prices are often $0; ``budget_mock_usd`` keeps demos / tests
    metering non-zero spend.
    """
    model_in = settings.parsed_model_cost_per_1k_input.get(model)
    model_out = settings.parsed_model_cost_per_1k_output.get(model)
    if model_in is not None or model_out is not None:
        in_rate = model_in if model_in is not None else 0.0
        out_rate = model_out if model_out is not None else 0.0
        cost = (prompt_tokens / 1000.0) * in_rate + (completion_tokens / 1000.0) * out_rate
    else:
        cost = estimate_response_cost_usd(
            provider=provider,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            settings=settings,
        )

    if cost <= 0.0 and provider == "mock":
        return settings.budget_mock_usd
    return cost
