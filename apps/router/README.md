# AI Inference Router

FastAPI service that selects a provider, talks to Bedrock / Vertex / mock adapters,
owns semantic caching, and enforces per-tenant USD/token budgets.

## Step 2 — routing

- **Policies:** `ROUTING_POLICY` = `prefer_cost` | `prefer_latency` | `prefer_provider` | `failover`
- **Failover:** primary (`ROUTING_PRIMARY`) then `ROUTING_FALLBACK` chain, with per-call
  timeout (`PROVIDER_TIMEOUT_MS`) and a circuit breaker
- **Model map:** `MODEL_MAP` maps logical names like `gpt-proxy` to Bedrock Claude /
  Vertex Gemini ids
- **Modes:** `PROVIDER_MODE=mock|bedrock|vertex|multi` (default `mock` needs no cloud keys)

Responses include `provider` and `route_reason` (`cost` / `latency` / `affinity` / `failover`).
Route decisions are also logged.

## Step 5 — budgets

- Redis counters: USD + tokens per minute/day/month (`budget:{tenant}:…`)
- Soft warning (`X-Budget-Warning: soft`) at `BUDGET_SOFT_RATIO`; hard reject with
  `BUDGET_HARD_STATUS` (default 402)
- Admin: `GET /v1/tenants/{id}/usage`, `GET /v1/tenants/{id}/budget`
- Spend events logged as structured `spend_audit` lines

Optional extras: `pip install -e ".[bedrock]"`, `".[vertex]"`, or `".[cloud]"`.

See the repo root `README.md` for architecture and curl examples.
