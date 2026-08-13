# Helm chart (kind / local Kubernetes)

Minimal chart to run gateway + router + Redis with **mock providers**.
Useful for a local multi-node demo without cloud credentials.

## Prerequisites

- [kind](https://kind.sigs.k8s.io/) (or any cluster)
- [Helm 3](https://helm.sh/)
- Docker (to build images)

## kind demo

```bash
# from repo root
kind create cluster --name ai-inference

docker build -t ai-gateway:local ./apps/gateway
docker build -t ai-router:local ./apps/router

kind load docker-image ai-gateway:local --name ai-inference
kind load docker-image ai-router:local --name ai-inference

helm upgrade --install aig ./deploy/helm/ai-inference-gateway \
  --set image.gateway.repository=ai-gateway \
  --set image.gateway.tag=local \
  --set image.router.repository=ai-router \
  --set image.router.tag=local

# NodePort 30080 on kind nodes — map via:
kubectl port-forward svc/gateway 8080:8080
curl -s http://127.0.0.1:8080/health
curl -s http://127.0.0.1:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-API-Key: demo-key-change-me" \
  -d '{"model":"mock-small","messages":[{"role":"user","content":"kind demo"}]}'
```

## Cloud providers on Kubernetes

1. Rebuild router with `INSTALL_EXTRAS='[bedrock]'`, `[vertex]`, or `[cloud]`.
2. Set `router.providerMode` and cloud env via values / External Secrets.
3. Bind IRSA (EKS) or Workload Identity (GKE) as described in
   [`../aws/README.md`](../aws/README.md) and [`../gcp/README.md`](../gcp/README.md).

Do not put long-lived cloud keys in `values.yaml` committed to git.

The chart defaults to `redis:8-alpine` so the router can use HNSW cache lookup
(`CACHE_INDEX_BACKEND=auto`). Override `redis.image=redis:7-alpine` and
`router.cacheIndexBackend=scan` if you need vanilla Redis.
