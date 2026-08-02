# AWS deploy (Bedrock)

Run the stack in **ECS Fargate** (primary sketch) with the router calling
**Amazon Bedrock** in-region. EKS notes at the bottom if you prefer Kubernetes.

## Architecture

```
ALB → ECS service: gateway
         │
         └─▶ ECS service: router  ──▶ ElastiCache Redis
                        │
                        └─▶ Bedrock Runtime (same region)
```

Use a private subnet for router + Redis; put the gateway behind an ALB
(HTTP → container `:8080`). Do not publish the router or Redis to the internet.

## Prerequisites

- AWS account with Bedrock model access enabled for your target models
- ECR repositories for `ai-gateway` and `ai-router`
- VPC with public + private subnets, NAT for Fargate pulls
- ElastiCache Redis (or MemoryDB) reachable from the router task

## Build & push

```bash
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
AWS_REGION="${AWS_REGION:-us-east-1}"
ECR="$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"

aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ECR"

docker build -t "$ECR/ai-gateway:latest" ./apps/gateway
docker build \
  --build-arg INSTALL_EXTRAS='[bedrock]' \
  -t "$ECR/ai-router:latest" \
  ./apps/router

docker push "$ECR/ai-gateway:latest"
docker push "$ECR/ai-router:latest"
```

## Secrets

Store in **Secrets Manager** or **SSM Parameter Store** (SecureString), then
reference from the task definition (`secrets:` entries). Never bake into images.

| Secret | Used by |
|--------|---------|
| `DEMO_API_KEY` or `TENANT_API_KEYS` | gateway |
| Optional static AWS keys | **prefer task role instead** |

Prefer an **ECS task IAM role** for Bedrock (no long-lived access keys).

## IAM (least privilege)

Attach the task role policy sketch in [`iam-task-role.json`](iam-task-role.json)
to the **router** task role. Gateway needs no Bedrock permissions.

Summary:

- `bedrock:InvokeModel`
- `bedrock:InvokeModelWithResponseStream`
- Resource: model ARNs you actually use (or `*` scoped by region if models vary)

Also grant the execution role the usual ECR pull + CloudWatch Logs +
`secretsmanager:GetSecretValue` / `ssm:GetParameters` for injected secrets.

## ECS task definitions

See [`ecs-task-definitions.sketch.json`](ecs-task-definitions.sketch.json).
Replace placeholders (`ACCOUNT`, `REGION`, secret ARNs, Redis endpoint).

Important env for Bedrock:

| Variable | Example |
|----------|---------|
| `PROVIDER_MODE` | `bedrock` or `multi` |
| `AWS_REGION` | `us-east-1` |
| `ROUTING_PRIMARY` | `bedrock` |
| `ROUTING_FALLBACK` | `mock` (or `vertex,mock` if dual-cloud) |
| `MODEL_MAP` | map logical models → Bedrock model ids |
| `REDIS_URL` | `redis://your-elasticache:6379/0` |

With a task role, omit `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`.

## Networking checklist

- [ ] Security group: ALB → gateway `:8080`
- [ ] Security group: gateway → router `:8081`
- [ ] Security group: router → Redis `:6379`
- [ ] Router egress to Bedrock Runtime (HTTPS) in-region
- [ ] Health checks: gateway/router `GET /health`

## Multi-cloud from AWS

Set `PROVIDER_MODE=multi`, build with `INSTALL_EXTRAS='[cloud]'`, and inject
GCP credentials (e.g. mount a Secret Manager JSON as
`GOOGLE_APPLICATION_CREDENTIALS`). Prefer keeping Vertex calls in GCP for
latency; use AWS as the Bedrock edge when most traffic is US-East Bedrock.

## EKS (optional)

Use the [Helm chart](../helm/) with:

- IRSA role annotated on the router ServiceAccount (same Bedrock actions)
- External Secrets Operator → Secrets Manager / SSM
- ElastiCache or in-cluster Redis for demos only

## Verify

```bash
curl -s https://<alb-dns>/health
curl -s https://<alb-dns>/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <key>" \
  -d '{"model":"gpt-proxy","messages":[{"role":"user","content":"hello from aws"}]}'
```
