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


_BUDGET_LIMIT_KEYS = (
    "usd_minute",
    "usd_day",
    "usd_month",
    "tokens_minute",
    "tokens_day",
    "tokens_month",
)


def _parse_tenant_budgets(raw: str) -> dict[str, dict[str, float]]:
    """Parse per-tenant budget overrides.

    Formats:
    - JSON: {"acme":{"usd_day":1.0,"tokens_day":5000}}
    - Compact: acme:usd_day:1.0,tokens_day:5000;beta:usd_month:50
    """
    raw = raw.strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        parsed = json.loads(raw)
        out: dict[str, dict[str, float]] = {}
        for tenant, limits in parsed.items():
            out[str(tenant)] = {str(k): float(v) for k, v in limits.items()}
        return out

    out = {}
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        tenant, _, rest = entry.partition(":")
        if not tenant or not rest:
            continue
        limits: dict[str, float] = {}
        for part in rest.split(","):
            part = part.strip()
            if not part:
                continue
            # key:value where key may contain underscores (usd_day:1.0)
            key, _, value = part.partition(":")
            if key and value:
                limits[key.strip()] = float(value.strip())
        if limits:
            out[tenant.strip()] = limits
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

    # Semantic cache
    cache_enabled: bool = True
    cache_similarity_threshold: float = 0.90
    cache_ttl_seconds: int = 3600
    cache_max_entries: int = 1000
    cache_embedding_provider: str = "hashing"  # hashing | sentence-transformers
    cache_embedding_dim: int = 256
    # Fallback USD/request used for mock (zero list price) so demos still show $ saved
    cache_mock_savings_usd: float = 0.002

    # Optional per-model pricing overrides (take precedence over provider rates)
    model_cost_per_1k_input: str = ""
    model_cost_per_1k_output: str = ""

    # Per-tenant budgeting
    budget_enabled: bool = True
    budget_soft_ratio: float = 0.8
    budget_hard_status: int = 402  # 402 Payment Required | 429 Too Many Requests
    # Stand-in USD/request for mock when list price is $0
    budget_mock_usd: float = 0.002
    budget_usd_per_minute: float | None = None
    budget_usd_per_day: float | None = 10.0
    budget_usd_per_month: float | None = 100.0
    budget_tokens_per_minute: float | None = None
    budget_tokens_per_day: float | None = 1_000_000.0
    budget_tokens_per_month: float | None = 10_000_000.0
    # Per-tenant overrides: acme:usd_day:1.0,tokens_day:5000;beta:usd_month:50
    tenant_budgets: str = ""

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
    def parsed_model_cost_per_1k_input(self) -> dict[str, float]:
        return _parse_float_map(self.model_cost_per_1k_input)

    @property
    def parsed_model_cost_per_1k_output(self) -> dict[str, float]:
        return _parse_float_map(self.model_cost_per_1k_output)

    @property
    def latency_hints_ms(self) -> dict[str, float]:
        return _parse_float_map(self.provider_latency_ms)

    @property
    def parsed_model_map(self) -> dict[str, dict[str, str]]:
        return _parse_model_map(self.model_map)

    @property
    def fallback_providers(self) -> list[str]:
        return _parse_csv(self.routing_fallback)

    @property
    def parsed_tenant_budgets(self) -> dict[str, dict[str, float]]:
        return _parse_tenant_budgets(self.tenant_budgets)

    def default_budget_limits(self) -> dict[str, float | None]:
        return {
            "usd_minute": self.budget_usd_per_minute,
            "usd_day": self.budget_usd_per_day,
            "usd_month": self.budget_usd_per_month,
            "tokens_minute": self.budget_tokens_per_minute,
            "tokens_day": self.budget_tokens_per_day,
            "tokens_month": self.budget_tokens_per_month,
        }

    def budget_for_tenant(self, tenant: str) -> dict[str, float | None]:
        """Merged default + per-tenant limit overrides (None = unlimited)."""
        limits = dict(self.default_budget_limits())
        overrides = self.parsed_tenant_budgets.get(tenant, {})
        for key in _BUDGET_LIMIT_KEYS:
            if key in overrides:
                limits[key] = overrides[key]
        return limits

    def resolve_physical_model(self, logical: str, provider: str) -> str:
        """Map a logical model name to the provider-specific id (fallback: as-is)."""
        aliases = self.parsed_model_map.get(logical, {})
        return aliases.get(provider, logical)


settings = Settings()
