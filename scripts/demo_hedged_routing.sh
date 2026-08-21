#!/usr/bin/env bash
# Demo: hedged routing races the next provider if the first is still in-flight.
#
# Default compose is HEDGE_AFTER_MS=0 (sequential failover). To see a faster
# peer win without cloud credentials, restart with:
#
#   MOCK_LATENCY_MS=150 MOCK_PEER_LATENCY_MS=15 HEDGE_AFTER_MS=40 \
#     CACHE_ENABLED=false docker compose up --build
#
# Hedging is a latency trade: the cancelled peer may still consume some
# provider work. Metering still bills the kept completion only.
set -euo pipefail

GATEWAY="${GATEWAY_URL:-http://localhost:18080}"
ROUTER="${ROUTER_URL:-http://localhost:18081}"
API_KEY="${DEMO_API_KEY:-demo-key-change-me}"
N="${HEDGE_DEMO_REQUESTS:-4}"

echo "== Hedged routing demo =="
echo "Gateway: ${GATEWAY}"
echo

health="$(curl -sS "${ROUTER}/health")"
eval "$(python3 -c "
import json,sys
h=json.loads(sys.argv[1])
hedge=h.get('hedge') or {}
print(f\"after_ms={hedge.get('after_ms')!r}; enabled={hedge.get('enabled')!r}; providers={list((h.get('providers') or {}).keys())!r}\")
" "${health}")"

echo "HEDGE_AFTER_MS=${after_ms} enabled=${enabled} providers=${providers}"
if [[ "${enabled}" != "True" && "${enabled}" != "true" ]]; then
  echo
  echo "Hedge is off (HEDGE_AFTER_MS=0). Restart with the command in the header of this script, then retry."
  exit 1
fi
if [[ "${providers}" != *"mock-peer"* ]]; then
  echo
  echo "Expected mock-peer in PROVIDER_MODE=mock (set MOCK_PEER_LATENCY_MS). Current providers=${providers}"
  exit 1
fi

echo
echo "Firing ${N} cache-bypassed completions..."
echo

hedged=0
for i in $(seq 1 "${N}"); do
  BODY="$(mktemp)"
  out="$(curl -sS -o "$BODY" -w '%{http_code} %{time_total}' \
    "${GATEWAY}/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -H "X-API-Key: ${API_KEY}" \
    -H "X-Cache-Bypass: 1" \
    -d "{\"model\":\"mock-small\",\"messages\":[{\"role\":\"user\",\"content\":\"hedge demo ${i} ${RANDOM}\"}]}")"
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
  if [[ "${reason}" == "hedged" ]]; then
    hedged=$((hedged + 1))
  fi
  rm -f "$BODY"
done

echo
echo "== Routing stats (GET /v1/routing/stats) =="
curl -sS "${ROUTER}/v1/routing/stats" | python3 -m json.tool
echo
if [[ "${hedged}" -lt 1 ]]; then
  echo "Expected at least one route_reason=hedged (got ${hedged}/${N})."
  echo "Is MOCK_LATENCY_MS much larger than HEDGE_AFTER_MS + MOCK_PEER_LATENCY_MS?"
  exit 1
fi
echo "Grafana (monitoring profile): Hedged routing (fired / won / cancelled)"
echo "Prometheus: curl -s ${ROUTER}/metrics | grep router_hedge"
echo "OK: hedge raced a secondary and kept the first success (${hedged}/${N} hedged)."
