# Live path: ECS Fargate + Amazon Bedrock

This is the **verified live cloud path** for calling a real provider. Local Compose
and CI stay on `PROVIDER_MODE=mock` (no cloud keys). GCP Cloud Run + Vertex remains
a sketch under [`../gcp/`](../gcp/); Helm/kind stays mock-first under [`../helm/`](../helm/).

**Proof of a live call:** HTTP 200, `"provider":"bedrock"`, completion `id` prefixed
`chatcmpl-bedrock-`, and content that is not the mock echo. Capture that with
[`../../scripts/verify_live_bedrock.sh`](../../scripts/verify_live_bedrock.sh).

---

## Path A — Local dry-run (Compose + Bedrock)

Use this before ECS to confirm credentials, model access, and the router image
extras. Secrets stay in your shell / `.env` (gitignored), never in git.

```bash
# from repo root
cp -n .env.example .env   # if needed

# AWS credentials (prefer a profile / SSO session over long-lived keys)
export AWS_REGION=us-east-1
# export AWS_PROFILE=your-profile
# or: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY [/ AWS_SESSION_TOKEN]

# Enable Bedrock model access in the console for the model id in MODEL_MAP
# (default: anthropic.claude-3-haiku-20240307-v1:0).

docker compose -f docker-compose.yml -f docker-compose.bedrock.yml up --build -d

chmod +x scripts/verify_live_bedrock.sh
./scripts/verify_live_bedrock.sh
```

The overlay builds the router with `INSTALL_EXTRAS='[bedrock]'`, sets
`PROVIDER_MODE=bedrock`, and disables cache so the verify script proves a live
invoke. Tear down with `docker compose down` when finished; resume mock mode with
plain `docker compose up --build`.

---

## Path B — ECS Fargate (production-shaped)

### 1. Secrets (Secrets Manager)

```bash
AWS_REGION="${AWS_REGION:-us-east-1}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"

# Gateway API key — do not commit the value
aws secretsmanager create-secret \
  --name ai-gateway/demo-api-key \
  --secret-string "${DEMO_API_KEY:?set DEMO_API_KEY}" \
  --region "$AWS_REGION"
```

Prefer an **ECS task IAM role** for Bedrock (see [`iam-task-role.json`](iam-task-role.json)).
Do **not** store `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` in Secrets Manager for
the happy path.

### 2. Build & push (Bedrock extras)

```bash
ECR="${ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com"
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ECR"

docker build -t "${ECR}/ai-gateway:latest" ./apps/gateway
docker build \
  --build-arg INSTALL_EXTRAS='[bedrock]' \
  -t "${ECR}/ai-router:latest" \
  ./apps/router

docker push "${ECR}/ai-gateway:latest"
docker push "${ECR}/ai-router:latest"
```

### 3. Task definitions

Copy [`ecs-task-definitions.sketch.json`](ecs-task-definitions.sketch.json), replace
`ACCOUNT` / `REGION` / Redis host / secret ARNs, then register:

```bash
# After editing placeholders into gateway-task.json / router-task.json:
aws ecs register-task-definition --cli-input-json file://gateway-task.json
aws ecs register-task-definition --cli-input-json file://router-task.json
```

Required runtime shape:

| Service | Must have |
|---------|-----------|
| **gateway** | `DEMO_API_KEY` (secret), `ROUTER_URL`, `REDIS_URL`, `RATE_LIMIT_*` |
| **router** | `PROVIDER_MODE=bedrock`, `AWS_REGION`, `REDIS_URL`, `MODEL_MAP`, task role with Bedrock invoke |

### 4. Networking checklist

- [ ] ALB → gateway `:8080`
- [ ] Gateway → router `:8081`
- [ ] Gateway → Redis `:6379` (rate-limit buckets)
- [ ] Router → Redis `:6379` (cache + budgets)
- [ ] Router egress HTTPS to Bedrock Runtime in-region
- [ ] Health checks: `GET /health` on gateway and router

### 5. Verify against the ALB

```bash
export GATEWAY_URL="https://<alb-dns>"
export DEMO_API_KEY="…"   # same value stored in Secrets Manager

curl -sS "${GATEWAY_URL}/health"

./scripts/verify_live_bedrock.sh
# Expect: PASS: live Bedrock response confirmed (provider=bedrock).
```

Save the script’s evidence JSON under `deploy/aws/evidence/` (gitignored) or paste a
**redacted** snippet into your run notes:

```json
{
  "provider": "bedrock",
  "route_reason": "failover",
  "cached": false,
  "id": "chatcmpl-bedrock-…",
  "content_preview": "bedrock-live-ok"
}
```

Reject the run if `provider` is `mock`, the body matches `[mock:…] Echo (…)`, or the
call only succeeded via cache.

---

## Mock / CI unchanged

| Environment | Provider |
|-------------|----------|
| `docker compose up` (no overlay) | `mock` |
| GitHub Actions compose smoke | `mock` |
| Helm / kind defaults | `mock` |
| This live path | `bedrock` |

---

## Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| `boto3 is required for Bedrock` | Image built without `INSTALL_EXTRAS='[bedrock]'` |
| AccessDeniedException | Task role missing invoke; or model access not enabled |
| ValidationException / model id errors | `MODEL_MAP` physical id not available in region |
| `provider=mock` | Overlay not applied, or failover exhausted Bedrock and hit fallback |
| Verify fails on `cached=true` | Disable cache or keep `X-Cache-Bypass: 1` |
