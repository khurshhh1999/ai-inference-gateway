# AI Inference Gateway (edge)

TypeScript Fastify edge that authenticates callers and proxies chat completions
to the FastAPI router.

- Auth: `X-API-Key` (timing-safe) → tenant via `TENANT_API_KEYS`
- Rate limit: Redis (or in-memory) token bucket per API key and/or tenant
  (`RATE_LIMIT_*`); HTTP **429** + `X-RateLimit-*` / `Retry-After`
- Hardening: `MAX_BODY_BYTES` (default 256KiB → HTTP 413)
- Observability: `GET /metrics`, OpenAPI at `/openapi.json` + `/docs`
- Proxies JSON + SSE without buffering the stream body

**Rate limit vs budget:** rate limits cap request concurrency/QPS at the edge;
router budgets cap USD/token spend. Both can return 429 depending on config.
