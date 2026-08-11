# AI Inference Gateway

A multi-cloud **LLM inference gateway** that fronts **AWS Bedrock** and **GCP Vertex AI**
behind one API — with **semantic caching**, **streaming**, **per-tenant budgeting**,
and **edge rate limiting**.

Built to cut model spend and latency for multi-tenant AI products: target **~40% lower
model cost** and **~35% lower p95** on cacheable traffic (see [load/RESULTS.md](load/RESULTS.md)).

## Architecture

```
┌──────────────┐     ┌─────────────────┐     ┌──────────────────┐
│   Clients    │────▶│  gateway (TS)   │────▶│  router (FastAPI)│
│              │     │  :8080          │     │  :8081           │
└──────────────┘     └────────┬────────┘     └────────┬─────────┘
                              │                       │
                              │         ┌─────────────┴────────┐
                              └────────▶│ Redis                │
                                        │ cache + budgets + RL │
                                        └──────────┬───────────┘
                                                   │
                     ┌─────────────────┬───────────┴───────────┐
                     │ AWS Bedrock     │ GCP Vertex AI │ Mock  │
                     └─────────────────┴───────────────┴───────┘
```

| Service | Port | Responsibility |
|---------|------|----------------|
| **gateway** | 8080 (host **18080**) | Public REST + SSE edge (TypeScript / Fastify) |
| **router** | 8081 (host **18081**) | Routing, providers, semantic cache, budgets (FastAPI) |
| **Redis** | 6379 (internal) | Cache vectors, budget counters, gateway rate-limit buckets |
| **Prometheus** | 9090 (host **19090**) | Metrics scrape (compose profile `monitoring`) |
| **Grafana** | 3000 (host **13000**) | Dashboards (compose profile `monitoring`) |
| **Jaeger** | 16686 (host **16686**) | Trace UI (compose profile `monitoring`) |

## Key features

| Feature | Status |
|---------|--------|
| OpenAI-shaped `POST /v1/chat/completions` | Done |
| Mock provider for local / CI (no cloud keys) | Done |
| Multi-provider routing (Bedrock + Vertex) + failover | Done |
| Semantic response cache (Redis) | Done |
| SSE token streaming | Done |
| Per-tenant API keys + USD/token budgets | Done |
| Gateway per-key / per-tenant rate limiting | Done |
| Prometheus metrics + Grafana + k6 p95 evidence | Done |
| OpenTelemetry traces + `X-Request-Id` correlation | Done |
| Multi-cloud deploy sketches (AWS + GCP + Helm) | Done |

## Quick start

### Prerequisites

- Docker & Docker Compose
- (Optional for local dev) Node 20+, Python 3.12+
- (Optional) [k6](https://k6.io) for load tests

### Run the stack

```bash
cp .env.example .env
docker compose up --build
```

Gateway is published on **http://localhost:18080** by default (router on **18081**)
so it does not collide with other local stacks on 8080/8081. Override with
`GATEWAY_HOST_PORT` / `ROUTER_HOST_PORT` in `.env`.

With observability:

```bash
# Optional: export traces to Jaeger (compose network)
# echo 'OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318' >> .env

docker compose --profile monitoring up --build
# Prometheus http://localhost:19090
# Grafana    http://localhost:13000  (admin / admin by default)
# Jaeger UI  http://localhost:16686
```

### Chat completion (mock)

```bash
curl -s http://localhost:18080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-API-Key: demo-key-change-me" \
  -d '{
    "model": "mock-small",
    "messages": [{"role": "user", "content": "Explain semantic caching in one sentence."}]
  }' | jq .
```

### Streaming (`stream: true`)

OpenAI-shaped SSE: incremental `chat.completion.chunk` frames, then `data: [DONE]`.
Use `curl -N` so the client does not buffer the stream. The gateway pipes events
without assembling the full body; disconnecting the client aborts the upstream
router/provider stream. Completed streams are eligible for the semantic cache
(same as non-stream); mid-stream aborts are not stored.

```bash
curl -N -s http://localhost:18080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-API-Key: demo-key-change-me" \
  -d '{
    "model": "mock-small",
    "stream": true,
    "messages": [{"role": "user", "content": "Stream a short hello."}]
  }'

# or
chmod +x scripts/demo_streaming.sh
./scripts/demo_streaming.sh
```

### Health, metrics, OpenAPI, tracing

```bash
curl -s http://localhost:18080/health
curl -s http://localhost:18081/health
curl -s http://localhost:18080/metrics | head
curl -s http://localhost:18081/metrics | head
curl -s http://localhost:18080/openapi.json | jq .info
# Swagger UI: http://localhost:18080/docs
curl -s http://localhost:18081/v1/cache/stats
curl -s http://localhost:18081/v1/tenants/default/usage
curl -s http://localhost:18081/v1/tenants/default/budget
```

Every request gets an `X-Request-Id` (client-supplied or generated). The gateway
forwards it to the router, echoes it on the response, and attaches it to
OpenTelemetry spans together with W3C `traceparent` propagation
(gateway → router → provider attempt). Spans are created whenever
`OTEL_ENABLED=true` (default). Set `OTEL_EXPORTER_OTLP_ENDPOINT` to export via
OTLP HTTP (e.g. `http://jaeger:4318` with the monitoring profile).

```bash
# Echo / round-trip a correlation id
curl -sD - -o /dev/null http://localhost:18080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-API-Key: demo-key-change-me" \
  -H "X-Request-Id: demo-trace-001" \
  -d '{"model":"mock-small","messages":[{"role":"user","content":"hi"}]}' \
  | grep -i x-request-id

chmod +x scripts/demo_tracing.sh
./scripts/demo_tracing.sh
```

Stable Prometheus names use `gateway_*` / `router_*` prefixes with explicit
histogram buckets for p95. Grafana dashboard **AI Inference Gateway Overview**
is provisioned under the `monitoring` profile; alerts cover error rate, miss
p95, budget burn, and provider errors (`monitoring/alerts.yml`). Jaeger is
included in the same profile for distributed traces.

### Per-tenant budgets

Gateway API keys map to tenants (`TENANT_API_KEYS=key:tenant,...`; default
`DEMO_API_KEY` → `default`). The router meters USD + tokens in Redis per
minute/day/month window using config-driven pricing (provider rates, optional
per-model overrides; mock uses `BUDGET_MOCK_USD`). Soft threshold
(`BUDGET_SOFT_RATIO`) sets `X-Budget-Warning: soft`; hard limit returns
`BUDGET_HARD_STATUS` (default **402**). Cache hits do not increment spend.

```bash
chmod +x scripts/demo_budgets.sh
./scripts/demo_budgets.sh
```

### Gateway rate limiting

Token-bucket limits at the edge (`RATE_LIMIT_KEY_QPS` / `RATE_LIMIT_KEY_BURST`,
optional `RATE_LIMIT_TENANT_*`). Over-limit keys get **429** with
`X-RateLimit-*` and `Retry-After`. **Rate limit = concurrency/QPS; budget =
spend** — they complement each other. Redis is the default backend (fail-open
on brief Redis errors); use `RATE_LIMIT_BACKEND=memory` for local unit tests.

```bash
# Clear demo: restart with a tight burst, then:
# RATE_LIMIT_KEY_QPS=2 RATE_LIMIT_KEY_BURST=3 docker compose up --build -d gateway
chmod +x scripts/demo_rate_limit.sh
./scripts/demo_rate_limit.sh
```

### Semantic cache demo

Near-duplicate prompts (same tenant + model family) return a cached completion
when similarity ≥ `CACHE_SIMILARITY_THRESHOLD` (default `0.90`: max of embedding
cosine and string near-duplicate ratio). Scope the cache with `X-Tenant-Id`
(default `default`); skip with `X-Cache-Bypass: 1`.

```bash
chmod +x scripts/demo_semantic_cache.sh
./scripts/demo_semantic_cache.sh
```

The script prints per-request `cached` / `route_reason` and router stats
(`cache_hit_total`, `cache_miss_total`, `estimated_usd_saved`).

### Load test (p95 evidence)

```bash
# stack must be up on :18080
chmod +x load/run.sh
./load/run.sh
```

This runs k6 `SCENARIO=compare`: baseline (cache bypass + unique prompts) then
cached (warmed repeated prompt), and refreshes [load/RESULTS.md](load/RESULTS.md).
Acceptance target: **≥35% p95 improvement** on the cacheable mix.

## Project layout

```
apps/gateway/     # TypeScript edge API
apps/router/      # FastAPI routing engine
packages/shared/  # Shared OpenAPI / schemas
monitoring/       # Prometheus + Grafana + alerts
load/             # k6 scripts + RESULTS.md
deploy/           # AWS / GCP / Helm sketches
```

## Deploy (AWS + GCP)

Sketches live under [`deploy/`](deploy/) — not a turnkey production platform:

| Path | Runtime | Provider focus |
|------|---------|----------------|
| [`deploy/aws/`](deploy/aws/) | ECS Fargate (+ EKS notes) | Bedrock + IAM least privilege |
| [`deploy/gcp/`](deploy/gcp/) | Cloud Run (+ GKE notes) | Vertex AI + IAM least privilege |
| [`deploy/helm/`](deploy/helm/) | kind / any Kubernetes | mock by default |

Build the router with cloud SDKs only when needed:

```bash
docker build --build-arg INSTALL_EXTRAS='[bedrock]' -t ai-router:bedrock ./apps/router
docker build --build-arg INSTALL_EXTRAS='[vertex]' -t ai-router:vertex ./apps/router
docker build --build-arg INSTALL_EXTRAS='[cloud]' -t ai-router:cloud ./apps/router
```

Inject API keys and cloud credentials from the platform secret store
(AWS Secrets Manager / SSM, GCP Secret Manager). No secrets belong in git.
See [`deploy/README.md`](deploy/README.md) for topology, IAM, and verify curls.

## Configuration

Copy `.env.example` → `.env`. Important variables:

| Variable | Default | Meaning |
|----------|---------|---------|
| `PROVIDER_MODE` | `mock` | `mock` \| `bedrock` \| `vertex` \| `multi` |
| `ROUTING_POLICY` | `failover` | `prefer_cost` \| `prefer_latency` \| `prefer_provider` \| `failover` |
| `ROUTING_PRIMARY` | `bedrock` | First provider for affinity / failover |
| `ROUTING_FALLBACK` | `vertex,mock` | Ordered failover chain after primary |
| `PROVIDER_TIMEOUT_MS` | `5000` | Per-provider call timeout |
| `MODEL_MAP` | (see `.env.example`) | Logical model → provider-specific ids |
| `DEMO_API_KEY` | `demo-key-change-me` | Gateway API key (maps to tenant `default` if `TENANT_API_KEYS` unset) |
| `TENANT_API_KEYS` | (empty) | `key:tenant,...` map for multi-tenant auth |
| `MAX_BODY_BYTES` | `262144` | Gateway JSON body limit (413 when exceeded) |
| `REDIS_URL` | `redis://redis:6379/0` | Cache / budget / rate-limit store |
| `RATE_LIMIT_ENABLED` | `true` | Gateway token-bucket rate limiting |
| `RATE_LIMIT_BACKEND` | `redis` | `redis` \| `memory` |
| `RATE_LIMIT_KEY_QPS` | `10` | Per-API-key refill rate (0 = disable key scope) |
| `RATE_LIMIT_KEY_BURST` | `20` | Per-API-key bucket capacity |
| `RATE_LIMIT_TENANT_QPS` | (empty/0) | Optional per-tenant QPS across keys |
| `RATE_LIMIT_TENANT_BURST` | (empty/0) | Optional per-tenant burst |
| `CACHE_ENABLED` | `true` | Semantic response cache |
| `CACHE_SIMILARITY_THRESHOLD` | `0.90` | Min similarity (embedding cosine or string ratio) |
| `CACHE_TTL_SECONDS` | `3600` | Entry TTL |
| `CACHE_MAX_ENTRIES` | `1000` | Per tenant+model family cap |
| `CACHE_EMBEDDING_PROVIDER` | `hashing` | `hashing` \| `sentence-transformers` |
| `BUDGET_ENABLED` | `true` | Per-tenant USD/token metering |
| `BUDGET_SOFT_RATIO` | `0.8` | Soft warning at this fraction of a hard limit |
| `BUDGET_HARD_STATUS` | `402` | HTTP status when budget exhausted (`402` or `429`) |
| `BUDGET_MOCK_USD` | `0.002` | Billable USD/request for mock (zero list price) |
| `BUDGET_USD_PER_DAY` | `10` | Default hard USD/day (override per tenant via `TENANT_BUDGETS`) |
| `BUDGET_TOKENS_PER_DAY` | `1000000` | Default hard tokens/day |
| `TENANT_BUDGETS` | (empty) | Per-tenant limit overrides |
| `ROUTER_URL` | `http://router:8081` | Gateway → router (Compose) |
| `GATEWAY_HOST_PORT` | `18080` | Host port for the gateway |
| `ROUTER_HOST_PORT` | `18081` | Host port for the router |
| `PROMETHEUS_HOST_PORT` | `19090` | Host port for Prometheus (`--profile monitoring`) |
| `GRAFANA_HOST_PORT` | `13000` | Host port for Grafana (`--profile monitoring`) |
| `JAEGER_UI_HOST_PORT` | `16686` | Host port for Jaeger UI (`--profile monitoring`) |
| `OTEL_ENABLED` | `true` | Create OpenTelemetry spans (gateway + router) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | (empty) | OTLP HTTP base URL (e.g. `http://jaeger:4318`); empty = no export |
| `OTEL_SERVICE_NAME_GATEWAY` | `gateway` | `service.name` for gateway spans |
| `OTEL_SERVICE_NAME_ROUTER` | `router` | `service.name` for router spans |
| `OTEL_CONSOLE_EXPORTER` | `false` | Print spans to stdout (debug) |

Cloud credentials (Bedrock / Vertex) are optional and only required when
`PROVIDER_MODE` is `bedrock`, `vertex`, or `multi` and you want real cloud calls.
With `PROVIDER_MODE=multi` and no creds, cloud adapters fail and the router
failsover to `mock` (local/CI stays green).

## Development

```bash
# Router
cd apps/router
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
ruff check app tests
pytest
uvicorn app.main:app --reload --port 8081

# Gateway
cd apps/gateway
npm install
npm test
npm run dev
```

CI (GitHub Actions) runs router lint + unit tests, gateway unit + build, and a
Docker Compose smoke (health, chat completion, `/metrics`, OpenAPI).

## License

MIT
