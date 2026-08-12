#!/usr/bin/env bash
# Demo: adaptive routing ranks providers from live EWMA latency + error rate.
#
# Default compose is PROVIDER_MODE=mock + ROUTING_POLICY=failover. To see a
# failing provider lose traffic (no routing-config edit), restart with:
#
#   PROVIDER_MODE=multi ROUTING_POLICY=adaptive CACHE_ENABLED=false \
#     docker compose up --build
#
# In multi mode without cloud creds, Bedrock/Vertex fail fast and the router
# learns to call mock first. Compare with ROUTING_POLICY=failover, which still
# tries the primary on every request.
set -euo pipefail

GATEWAY="${GATEWAY_URL:-http://localhost:18080}"
ROUTER="${ROUTER_URL:-http://localhost:18081}"
API_KEY="${DEMO_API_KEY:-demo-key-change-me}"
N="${ADAPTIVE_DEMO_REQUESTS:-6}"

echo "== Adaptive routing demo =="
echo "Gateway: ${GATEWAY}"
echo

health="$(curl -sS "${ROUTER}/health")"
policy="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('routing_policy',''))" "${health}")"
echo "Router policy: ${policy}"
if [[ "${policy}" != "adaptive" ]]; then
  echo
  echo "Router is not on ROUTING_POLICY=adaptive (current=${policy})."
  echo "Restart the stack with the command in the header of this script, then retry."
  exit 1
fi

echo
echo "Firing ${N} cache-bypassed completions (unique prompts)..."
echo

for i in $(seq 1 "${N}"); do
  BODY="$(mktemp)"
  out="$(curl -sS -o "$BODY" -w '%{http_code} %{time_total}' \
    "${GATEWAY}/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -H "X-API-Key: ${API_KEY}" \
    -H "X-Cache-Bypass: 1" \
    -d "{\"model\":\"mock-small\",\"messages\":[{\"role\":\"user\",\"content\":\"adaptive demo ${i} ${RANDOM}\"}]}")"
  code="${out%% *}"
  elapsed="${out##* }"
  eval "$(python3 -c "import json,sys; d=json.loads(open(sys.argv[1]).read()); print(f\"provider={d.get('provider')!r}; reason={d.get('route_reason')!r}\")" "$BODY")"
  echo "  #${i} HTTP ${code} provider=${provider} route_reason=${reason} ${elapsed}s"
  if [[ "${code}" != "200" ]]; then
    echo "Unexpected status; body:"
    cat "$BODY"
    rm -f "$BODY"
    exit 1
  fi
  if [[ "${reason}" != "adaptive" ]]; then
    echo "Expected route_reason=adaptive, got ${reason}"
    rm -f "$BODY"
    exit 1
  fi
  rm -f "$BODY"
done

echo
echo "== Live signals (GET /v1/routing/stats) =="
curl -sS "${ROUTER}/v1/routing/stats" | python3 -m json.tool
echo
echo "Grafana (monitoring profile): Adaptive EWMA latency / Adaptive error rate + score"
echo "Prometheus: curl -s ${ROUTER}/metrics | grep router_adaptive"
echo "OK: adaptive policy ranked from live EWMA (not only static PROVIDER_LATENCY_MS hints)."
