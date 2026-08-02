# Deploy sketches

Portfolio-oriented deploy paths for running the gateway near AWS Bedrock,
near GCP Vertex AI, or both. These are **sketches** (documented manifests +
IAM notes), not a turnkey production platform.

| Path | Cloud | Primary runtime | Provider |
|------|-------|-----------------|----------|
| [aws/](aws/) | AWS | ECS Fargate (EKS notes) | Bedrock |
| [gcp/](gcp/) | GCP | Cloud Run (GKE notes) | Vertex AI |
| [helm/](helm/) | local | kind / any Kubernetes | mock (default) |

## Principles

1. **Mock-first images** — default container builds need no cloud SDKs or
   credentials (same as local Compose / CI).
2. **Opt-in cloud deps** — build the router with `INSTALL_EXTRAS=[bedrock]`,
   `[vertex]`, or `[cloud]` when calling real providers.
3. **No secrets in git** — inject API keys and cloud credentials from the
   platform secret store (AWS Secrets Manager / SSM, GCP Secret Manager).
4. **Least privilege** — task/service identities get only the invoke APIs
   they need; see each cloud folder’s IAM notes.

## Suggested topology

```
Internet → gateway (public) → router (private) → Redis (private)
                                   ├─▶ Bedrock  (AWS region)
                                   └─▶ Vertex   (GCP region)
```

- Co-locate the **router** with the cloud model API you prefer for latency
  (`prefer_latency` / nearest region).
- Keep **Redis** in the same VPC / VPC connector as the router (cache + budgets).
- Expose only the **gateway** publicly; leave router and Redis internal.

## Build images

From the repo root (or CI):

```bash
# Mock / CI (default)
docker build -t ai-gateway:local ./apps/gateway
docker build -t ai-router:local ./apps/router

# Router with cloud SDKs
docker build \
  --build-arg INSTALL_EXTRAS='[cloud]' \
  -t ai-router:cloud \
  ./apps/router
```

Push to ECR (AWS) or Artifact Registry (GCP), then reference those tags from
the task/service definitions in `aws/` and `gcp/`.

## Required runtime config (minimum)

| Variable | Notes |
|----------|-------|
| `DEMO_API_KEY` / `TENANT_API_KEYS` | Gateway auth (from secret store) |
| `ROUTER_URL` | Internal URL to router |
| `REDIS_URL` | Internal Redis |
| `PROVIDER_MODE` | `mock` \| `bedrock` \| `vertex` \| `multi` |
| `ROUTING_*` / `MODEL_MAP` | As in root `.env.example` |

Cloud-specific vars are documented under [aws/](aws/) and [gcp/](gcp/).
