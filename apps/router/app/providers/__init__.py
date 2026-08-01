from __future__ import annotations

import logging

from app.config import Settings
from app.config import settings as default_settings
from app.providers.base import Provider
from app.providers.bedrock import BedrockProvider
from app.providers.mock import MockProvider
from app.providers.vertex import VertexProvider
from app.routing.engine import RoutingEngine

logger = logging.getLogger(__name__)

_engine: RoutingEngine | None = None


def build_providers(settings: Settings | None = None) -> dict[str, Provider]:
    cfg = settings or default_settings
    mode = cfg.provider_mode.lower().strip()

    if mode == "mock":
        return {"mock": MockProvider(latency_ms=cfg.mock_latency_ms)}

    if mode == "bedrock":
        return {"bedrock": BedrockProvider(region=cfg.aws_region)}

    if mode == "vertex":
        return {
            "vertex": VertexProvider(
                project=cfg.google_cloud_project or None,
                location=cfg.google_cloud_location,
            )
        }

    if mode == "multi":
        # Register all three. Cloud adapters fail (and failover) when creds/SDK missing.
        providers: dict[str, Provider] = {
            "bedrock": BedrockProvider(region=cfg.aws_region),
            "vertex": VertexProvider(
                project=cfg.google_cloud_project or None,
                location=cfg.google_cloud_location,
            ),
            "mock": MockProvider(latency_ms=cfg.mock_latency_ms),
        }
        return providers

    raise ValueError(f"Unsupported PROVIDER_MODE: {cfg.provider_mode}")


def get_routing_engine(settings: Settings | None = None) -> RoutingEngine:
    global _engine
    if settings is not None:
        return RoutingEngine(build_providers(settings), settings=settings)
    if _engine is None:
        _engine = RoutingEngine(build_providers(default_settings), settings=default_settings)
        logger.info(
            "routing engine ready mode=%s providers=%s policy=%s",
            default_settings.provider_mode,
            list(_engine.providers),
            default_settings.routing_policy,
        )
    return _engine


def reset_routing_engine() -> None:
    """Clear the cached engine (used by tests)."""
    global _engine
    _engine = None


# Back-compat for Step 1 call sites / health checks that expect a single provider.
def get_provider() -> Provider:
    engine = get_routing_engine()
    providers = engine.providers
    if "mock" in providers and len(providers) == 1:
        return providers["mock"]
    # Prefer primary when present, else first registered.
    primary = default_settings.routing_primary
    if primary in providers:
        return providers[primary]
    return next(iter(providers.values()))
