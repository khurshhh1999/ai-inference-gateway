# AI Inference Router

FastAPI service that selects a provider, talks to Bedrock / Vertex / mock adapters,
and (in later steps) owns semantic caching and tenant budgets.

## Step 2 — routing

- **Policies:** `ROUTING_POLICY` = `prefer_cost` | `prefer_latency` | `prefer_provider` | `failover`
- **Failover:** primary (`ROUTING_PRIMARY`) then `ROUTING_FALLBACK` chain, with per-call
  timeout (`PROVIDER_TIMEOUT_MS`) and a circuit breaker
- **Model map:** `MODEL_MAP` maps logical names like `gpt-proxy` to Bedrock Claude /
  Vertex Gemini ids
- **Modes:** `PROVIDER_MODE=mock|bedrock|vertex|multi` (default `mock` needs no cloud keys)

Responses include `provider` and `route_reason` (`cost` / `latency` / `affinity` / `failover`).
Route decisions are also logged.

Optional extras: `pip install -e ".[bedrock]"`, `".[vertex]"`, or `".[cloud]"`.

See the repo root `README.md` for architecture and curl examples.
