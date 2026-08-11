#!/usr/bin/env bash
# Demo: gateway Redis/memory token-bucket rate limits (per API key).
# Rate limit = concurrency/QPS; budget = USD/token spend (router).
#
# Tip: for a clear 429, run the stack with tight limits, e.g.:
#   RATE_LIMIT_KEY_QPS=2 RATE_LIMIT_KEY_BURST=3 docker compose up --build
# Or pass those env vars when starting the gateway locally.
set -euo pipefail

GATEWAY="${GATEWAY_URL:-http://localhost:18080}"
API_KEY="${DEMO_API_KEY:-demo-key-change-me}"
N="${RATE_LIMIT_DEMO_REQUESTS:-12}"

echo "== Gateway rate limit demo =="
echo "Gateway: ${GATEWAY}"
echo "Firing ${N} rapid requests (expect some 429 if burst is small)"
echo "Note: rate limit protects QPS; budgets (scripts/demo_budgets.sh) protect spend."
echo

ok=0
limited=0
other=0

for i in $(seq 1 "${N}"); do
  HEADERS="$(mktemp)"
  BODY="$(mktemp)"
  code="$(curl -sS -o "$BODY" -D "$HEADERS" -w '%{http_code}' \
    "${GATEWAY}/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -H "X-API-Key: ${API_KEY}" \
    -H "X-Cache-Bypass: 1" \
    -d "{\"model\":\"mock-small\",\"messages\":[{\"role\":\"user\",\"content\":\"rl demo ${i}\"}]}")"
  scope="$(grep -i '^x-ratelimit-scope:' "$HEADERS" | awk '{print $2}' | tr -d '\r' || true)"
  remaining="$(grep -i '^x-ratelimit-remaining:' "$HEADERS" | awk '{print $2}' | tr -d '\r' || true)"
  retry="$(grep -i '^retry-after:' "$HEADERS" | awk '{print $2}' | tr -d '\r' || true)"
  echo "  #${i} HTTP ${code} scope=${scope:--} remaining=${remaining:--} retry-after=${retry:--}"
  case "$code" in
    200) ok=$((ok + 1)) ;;
    429) limited=$((limited + 1)) ;;
    *) other=$((other + 1)) ;;
  esac
  rm -f "$HEADERS" "$BODY"
done

echo
echo "Summary: ok=${ok} rate_limited=${limited} other=${other}"
if [[ "$limited" -eq 0 ]]; then
  echo "No 429s observed. Tighten RATE_LIMIT_KEY_QPS / RATE_LIMIT_KEY_BURST and restart,"
  echo "or raise RATE_LIMIT_DEMO_REQUESTS (current=${N})."
  exit 1
fi

echo "OK: rate limit enforced (${limited} rejected)."
echo "Metrics: curl -s ${GATEWAY}/metrics | grep gateway_rate_limit"
