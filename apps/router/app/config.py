from __future__ import annotations

import json

from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_float_map(raw: str) -> dict[str, float]:
    """Parse `a:1.0,b:2.5` into a dict."""
    out: dict[str, float] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        name, _, value = part.partition(":")
        if not name or not value:
            continue
        out[name.strip()] = float(value.strip())
    return out


def _parse_csv(raw: str) -> list[str]:
    return [p.strip() for p in raw.split(",") if p.strip()]


def _parse_model_map(raw: str) -> dict[str, dict[str, str]]:
    """Parse logical model → {provider: physical_model}.

    Formats accepted:
    - JSON object: {"gpt-proxy":{"bedrock":"anthropic.claude-...","vertex":"gemini-1.5-flash"}}
    - Compact: gpt-proxy=bedrock:claude-haiku,vertex:gemini-1.5-flash;mock-small=mock:mock-small
    """
    raw = raw.strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        parsed = json.loads(raw)
        return {k: {pk: str(pv) for pk, pv in v.items()} for k, v in parsed.items()}

    out: dict[str, dict[str, str]] = {}
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        logical, _, rest = entry.partition("=")
        if not logical or not rest:
            continue
        providers: dict[str, str] = {}
        for mapping in rest.split(","):
            mapping = mapping.strip()
            provider, _, physical = mapping.partition(":")
            if provider and physical:
                providers[provider.strip()] = physical.strip()
        if providers:
            out[logical.strip()] = providers
    return out


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    provider_mode: str = "mock"
    redis_url: str = "redis://localhost:6379/0"
    log_level: str = "info"
    port: int = 8081
    mock_latency_ms: int = 40

    # Routing
    routing_policy: str = "failover"
    routing_primary: str = "bedrock"
    routing_fallback: str = "vertex,mock"
    provider_timeout_ms: int = 5_000

    # Circuit breaker
    circuit_breaker_failure_threshold: int = 3
    circuit_breaker_reset_ms: int = 30_000

    # Cost / latency hints used by prefer_cost / prefer_latency (and estimate_cost)
    provider_cost_per_1k_input: str = "mock:0.0,bedrock:0.00025,vertex:0.000075"
    provider_cost_per_1k_output: str = "mock:0.0,bedrock:0.00125,vertex:0.0003"
    provider_latency_ms: str = "mock:40,bedrock:200,vertex:150"

    # Logical model → provider-specific model ids
    model_map: str = (
        "gpt-proxy=bedrock:anthropic.claude-3-haiku-20240307-v1:0,"
        "vertex:gemini-1.5-flash;"
        "mock-small=mock:mock-small;"
        "claude-haiku=bedrock:anthropic.claude-3-haiku-20240307-v1:0;"
        "gemini-flash=vertex:gemini-1.5-flash"
    )

    # Cloud (optional — only needed when calling real Bedrock / Vertex)
    aws_region: str = "us-east-1"
    google_cloud_project: str = ""
    google_cloud_location: str = "us-central1"

    @property
    def cost_per_1k_input(self) -> dict[str, float]:
        return _parse_float_map(self.provider_cost_per_1k_input)

    @property
    def cost_per_1k_output(self) -> dict[str, float]:
        return _parse_float_map(self.provider_cost_per_1k_output)

    @property
    def latency_hints_ms(self) -> dict[str, float]:
        return _parse_float_map(self.provider_latency_ms)

    @property
    def parsed_model_map(self) -> dict[str, dict[str, str]]:
        return _parse_model_map(self.model_map)

    @property
    def fallback_providers(self) -> list[str]:
        return _parse_csv(self.routing_fallback)

    def resolve_physical_model(self, logical: str, provider: str) -> str:
        """Map a logical model name to the provider-specific id (fallback: as-is)."""
        aliases = self.parsed_model_map.get(logical, {})
        return aliases.get(provider, logical)


settings = Settings()
