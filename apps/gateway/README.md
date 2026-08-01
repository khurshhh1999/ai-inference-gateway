# AI Inference Gateway (edge)

TypeScript Fastify edge that authenticates callers and proxies chat completions
to the FastAPI router.

- Auth: `X-API-Key` (timing-safe) → tenant via `TENANT_API_KEYS`
- Hardening: `MAX_BODY_BYTES` (default 256KiB → HTTP 413)
- Observability: `GET /metrics`, OpenAPI at `/openapi.json` + `/docs`
- Proxies JSON + SSE without buffering the stream body
