# GCP deploy (Vertex AI)

Run the stack on **Cloud Run** (primary sketch) with the router calling
**Vertex AI** Gemini. GKE notes at the bottom if you prefer Kubernetes.

## Architecture

```
Internet → Cloud Run: gateway
              │
              └─▶ Cloud Run: router  ──▶ Memorystore Redis
                             │
                             └─▶ Vertex AI (same region / multi-region)
```

Keep the router **ingress restricted** (internal / load balancer only).
Expose only the gateway publicly. Attach a **Serverless VPC Access** connector
so Cloud Run can reach Memorystore. The **gateway** also needs Redis for
rate-limit buckets (see `REDIS_URL` / `RATE_LIMIT_*` in the sketch).

## Prerequisites

- GCP project with Vertex AI API enabled
- Artifact Registry repository for container images
- Memorystore for Redis (or a small GCE Redis for demos)
- Service accounts with least-privilege roles (see below)

## Build & push

```bash
PROJECT_ID="${GOOGLE_CLOUD_PROJECT:?set project}"
REGION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
REPO="${REGION}-docker.pkg.dev/${PROJECT_ID}/ai-inference"

gcloud auth configure-docker "${REGION}-docker.pkg.dev"

docker build -t "${REPO}/ai-gateway:latest" ./apps/gateway
docker build \
  --build-arg INSTALL_EXTRAS='[vertex]' \
  -t "${REPO}/ai-router:latest" \
  ./apps/router

docker push "${REPO}/ai-gateway:latest"
docker push "${REPO}/ai-router:latest"
```

## Secrets

Store API keys in **Secret Manager**. Mount or bind them as env vars on Cloud Run
(`--set-secrets`). Do not commit service-account JSON; prefer the attached
runtime service account.

| Secret | Used by |
|--------|---------|
| `DEMO_API_KEY` / `TENANT_API_KEYS` | gateway |
| Redis reachable from gateway + router | rate limits + cache/budgets |

## IAM (least privilege)

Create two service accounts:

| Account | Roles |
|---------|--------|
| `ai-gateway-sa` | `roles/run.invoker` on the **router** Cloud Run service (if private); Secret Manager accessor for API keys |
| `ai-router-sa` | `roles/aiplatform.user` (or custom role with predict/generate only); Secret Manager if needed; VPC access |

Avoid `roles/owner` / broad Editor. For dual-cloud from GCP, inject AWS keys via
Secret Manager only when calling Bedrock from this edge.

Custom role sketch: [`iam-router-custom-role.yaml`](iam-router-custom-role.yaml).

## Cloud Run services

Manifest sketches: [`cloud-run.sketch.yaml`](cloud-run.sketch.yaml).

Deploy example (gateway):

```bash
gcloud run deploy ai-gateway \
  --image="${REPO}/ai-gateway:latest" \
  --region="$REGION" \
  --allow-unauthenticated \
  --set-env-vars="ROUTER_URL=https://ai-router-XXXX-uc.a.run.app,PORT=8080" \
  --set-secrets="DEMO_API_KEY=demo-api-key:latest" \
  --service-account="ai-gateway-sa@${PROJECT_ID}.iam.gserviceaccount.com"
```

Router (private ingress + Vertex):

```bash
gcloud run deploy ai-router \
  --image="${REPO}/ai-router:latest" \
  --region="$REGION" \
  --ingress=internal \
  --vpc-connector=ai-inference-connector \
  --set-env-vars="PROVIDER_MODE=vertex,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION},REDIS_URL=redis://10.x.x.x:6379/0,PORT=8081" \
  --service-account="ai-router-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
  --no-allow-unauthenticated
```

If the router is private, gateway must use an identity token or be on a network
path that can reach internal Cloud Run (same project VPC / LB). For a simpler
demo, temporarily allow authenticated invoke from the gateway SA only.

Important env for Vertex:

| Variable | Example |
|----------|---------|
| `PROVIDER_MODE` | `vertex` or `multi` |
| `GOOGLE_CLOUD_PROJECT` | your project id |
| `GOOGLE_CLOUD_LOCATION` | `us-central1` |
| `ROUTING_PRIMARY` | `vertex` |
| `MODEL_MAP` | logical → `vertex:gemini-1.5-flash` |
| `REDIS_URL` | Memorystore private IP |

ADC via the Cloud Run service account — no `GOOGLE_APPLICATION_CREDENTIALS` file.

## GKE (optional)

Use the [Helm chart](../helm/) with:

- Workload Identity → `ai-router-sa` with `roles/aiplatform.user`
- External Secrets → Secret Manager
- Memorystore or in-cluster Redis for demos

## Multi-cloud from GCP

Build with `INSTALL_EXTRAS='[cloud]'`, set `PROVIDER_MODE=multi`, and inject
AWS credentials from Secret Manager when Bedrock failover is required. Prefer
GCP as the Vertex edge when most traffic is Gemini.

## Verify

```bash
curl -s https://<gateway-url>/health
curl -s https://<gateway-url>/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <key>" \
  -d '{"model":"gpt-proxy","messages":[{"role":"user","content":"hello from gcp"}]}'
```
