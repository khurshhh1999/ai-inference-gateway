# AI Inference Gateway

A multi-cloud **LLM inference gateway** that fronts **AWS Bedrock** and **GCP Vertex AI**
behind one API — with **semantic caching**, **streaming**, and **per-tenant budgeting**.

Built to cut model spend and latency for multi-tenant AI products: target **~40% lower
model cost** and **~35% lower p95** on cacheable traffic (see load results as they land).

## Architecture

```
┌──────────────┐     ┌─────────────────┐     ┌──────────────────┐
│   Clients    │────▶│  gateway (TS)   │────▶│  router (FastAPI)│
│              │     │  :8080          │     │  :8081           │
└──────────────┘     └────────┬────────┘     └────────┬─────────┘
                              │                       │
                              │              ┌────────┴────────┐
                              │              │ Redis           │
                              │              │ cache + budgets │
                              │              └────────┬────────┘
                              │                       │
                     ┌────────┴────────┬──────────────┴────────┐
                     │ AWS Bedrock     │ GCP Vertex AI │ Mock  │
                     └─────────────────┴───────────────┴───────┘
```

| Service | Port | Responsibility |
|---------|------|----------------|
| **gateway** | 8080 (host **18080**) | Public REST + SSE edge (TypeScript / Fastify) |
| **router** | 8081 (host **18081**) | Routing, providers, semantic cache, budgets (FastAPI) |
| **Redis** | 6379 (internal) | Cache vectors / keys + per-tenant usage counters |

## Key features

| Feature | Status |
|---------|--------|
| OpenAI-shaped `POST /v1/chat/completions` | Done (Step 1) |
| Mock provider for local / CI (no cloud keys) | Done |
| Multi-provider routing (Bedrock + Vertex) + failover | Done (Step 2) |
| Semantic response cache (Redis) | Done (Step 3) |
| SSE token streaming | Done (Step 4) |
| Per-tenant API keys + USD/token budgets | Done (Step 5) |
| Prometheus metrics + k6 load evidence | Planned |

## Quick start

### Prerequisites

- Docker & Docker Compose
- (Optional for local dev) Node 20+, Python 3.12+

### Run the stack

```bash
cp .env.example .env
docker compose up --build
```

Gateway is published on **http://localhost:18080** by default (router on **18081**)
so it does not collide with other local stacks on 8080/8081. Override with
`GATEWAY_HOST_PORT` / `ROUTER_HOST_PORT` in `.env`.

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

### Health

```bash
curl -s http://localhost:18080/health
curl -s http://localhost:18081/health
curl -s http://localhost:18081/v1/cache/stats
curl -s http://localhost:18081/v1/tenants/default/usage
curl -s http://localhost:18081/v1/tenants/default/budget
```

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

## Project layout

```
apps/gateway/     # TypeScript edge API
apps/router/      # FastAPI routing engine
packages/shared/  # Shared OpenAPI / schemas
monitoring/       # Prometheus / Grafana (later)
load/             # k6 scripts (later)
deploy/           # AWS / GCP / Helm sketches (later)
```

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
| `REDIS_URL` | `redis://redis:6379/0` | Cache / budget store |
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
pytest
uvicorn app.main:app --reload --port 8081

# Gateway
cd apps/gateway
npm install
npm test
npm run dev
```

## Roadmap (high level)

1. Foundation + mock providers ✅  
2. Bedrock / Vertex adapters + routing policies ✅  
3. Semantic caching ✅  
4. Streaming ✅  
5. Tenant budgets ✅  
6. Observability + load proof  
7. Multi-cloud deploy docs  

## License

MIT
