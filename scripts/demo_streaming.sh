#!/usr/bin/env bash
# Demo: SSE token streaming through gateway → router → mock (Step 4).
# Requires the stack: docker compose up --build
set -euo pipefail

GATEWAY="${GATEWAY_URL:-http://localhost:18080}"
API_KEY="${DEMO_API_KEY:-demo-key-change-me}"

echo "== Streaming (SSE) demo =="
echo "Gateway: ${GATEWAY}"
echo "Tip: curl -N disables buffering so tokens appear as they arrive."
echo

curl -N -s "${GATEWAY}/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -H "X-Cache-Bypass: 1" \
  -d '{
    "model": "mock-small",
    "stream": true,
    "messages": [{"role": "user", "content": "Stream a short hello."}]
  }'

echo
echo
echo "== Done (look for incremental data: frames ending with data: [DONE]) =="
