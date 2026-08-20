# AI Inference Router

FastAPI service that selects a provider, talks to Bedrock / Vertex / mock adapters,
owns semantic caching (scan or HNSW-indexed), and enforces per-tenant USD/token budgets.

## Routing

- **Policies:** `ROUTING_POLICY` = `prefer_cost` | `prefer_latency` | `prefer_provider` |
  `failover` | `adaptive`
- **Failover:** primary (`ROUTING_PRIMARY`) then `ROUTING_FALLBACK` chain, with per-call
  timeout (`PROVIDER_TIMEOUT_MS`) and a circuit breaker
- **Adaptive:** live EWMA latency + error rate per provider; static `PROVIDER_LATENCY_MS`
  hints are the cold start. Idle providers (`ADAPTIVE_STALE_AFTER_SECONDS`) are probed
  again. Snapshot: `GET /v1/routing/stats`
- **Model map:** `MODEL_MAP` maps logical names like `gpt-proxy` to Bedrock Claude /
  Vertex Gemini ids. Catalog: `GET /v1/models` (and `GET /v1/models/{id}`).
- **Modes:** `PROVIDER_MODE=mock|bedrock|vertex|multi` (default `mock` needs no cloud keys)

Responses include `provider` and `route_reason` (`cost` / `latency` / `affinity` /
`failover` / `adaptive`). Route decisions are also logged.

## Tool / function calling

`POST /v1/chat/completions` accepts OpenAI-shaped `tools` and `tool_choice`.
The mock adapter emits `finish_reason=tool_calls` when a tool is selected;
send the result back as `role: tool`. Bedrock maps to Claude `tool_use` /
`tool_result`; Vertex maps to Gemini function declarations. Tool-calling
turns are skipped by the semantic cache.

## Embeddings

`POST /v1/embeddings` is OpenAI-shaped (`model` + `input` string or list).
Vectors come from the same local embedder as the semantic cache
(`CACHE_EMBEDDING_PROVIDER`; hashing by default). Aliases:
`text-embedding-3-small`, `text-embedding-hashing`. Token usage is metered
against the tenant budget; hashing list price is $0 unless you set per-model rates.

## Semantic cache

- Namespace: tenant + model family. Never shared across tenants.
- Default embedder is hashing (no ML deps). Optional
  `CACHE_EMBEDDING_PROVIDER=sentence-transformers` via `pip install '.[embeddings]'`.
- `CACHE_INDEX_BACKEND=auto` (default): HNSW KNN when Redis Query Engine is loaded
  (Redis 8 / Redis Stack), otherwise an O(n) scan capped by `CACHE_MAX_ENTRIES`.
- Stats: `GET /v1/cache/stats` (`index_backend`, hit/miss, estimated USD saved).

## Budgets

- Redis counters: USD + tokens per minute/day/month (`budget:{tenant}:…`)
- Soft warning (`X-Budget-Warning: soft`) at `BUDGET_SOFT_RATIO`; hard reject with
  `BUDGET_HARD_STATUS` (default 402)
- Admin: `GET /v1/tenants/{id}/usage`, `GET /v1/tenants/{id}/budget`
- Spend events logged as structured `spend_audit` lines

## Cloud SDKs

Optional extras: `pip install -e ".[bedrock]"`, `".[vertex]"`, or `".[cloud]"`.

Docker image build arg `INSTALL_EXTRAS` (e.g. `[cloud]`) installs the same extras
for AWS/GCP deploys — see [`deploy/`](../../deploy/).

See the repo root `README.md` for architecture and curl examples.
