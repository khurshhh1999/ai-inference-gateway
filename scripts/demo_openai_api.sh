#!/usr/bin/env bash
# Demo: OpenAI-shaped GET /v1/models + POST /v1/embeddings through the gateway.
# Requires the stack: docker compose up --build
set -euo pipefail

GATEWAY="${GATEWAY_URL:-http://localhost:18080}"
API_KEY="${DEMO_API_KEY:-demo-key-change-me}"

echo "== Models (X-API-Key) =="
curl -sS "${GATEWAY}/v1/models" \
  -H "X-API-Key: ${API_KEY}" \
  -H "X-Request-Id: demo-models-001" | python3 -m json.tool

echo
echo "== Models (Authorization: Bearer) =="
curl -sS "${GATEWAY}/v1/models/text-embedding-3-small" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "X-Request-Id: demo-models-002" | python3 -m json.tool

echo
echo "== Embeddings =="
curl -sS "${GATEWAY}/v1/embeddings" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -H "X-Request-Id: demo-emb-001" \
  -d '{
    "model": "text-embedding-3-small",
    "input": ["semantic caching", "an unrelated prompt"]
  }' | python3 -c "
import json, sys, math
d = json.load(sys.stdin)
print('model=', d.get('model'))
print('provider=', d.get('embedding_provider'), 'dim=', d.get('dim'))
print('n=', len(d.get('data', [])), 'usage=', d.get('usage'))
for item in d.get('data', []):
    vec = item['embedding']
    norm = math.sqrt(sum(x*x for x in vec))
    print(f\"  index={item['index']} len={len(vec)} l2={norm:.6f}\")
"

echo
echo "Chat completions are unchanged — see README or scripts/demo_streaming.sh."
