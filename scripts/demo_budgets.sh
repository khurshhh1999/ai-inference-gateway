#!/usr/bin/env bash
# Demo: per-tenant budgets + cost metering.
# Requires the stack: docker compose up --build
set -euo pipefail

GATEWAY="${GATEWAY_URL:-http://localhost:18080}"
ROUTER="${ROUTER_URL_HOST:-http://localhost:18081}"
API_KEY="${DEMO_API_KEY:-demo-key-change-me}"
# Router-side tenant (gateway maps API key → tenant; for direct router curls use header)
TENANT="${TENANT_ID:-demo-budget}"

echo "== Budget metering demo =="
echo "Gateway: ${GATEWAY}  Router: ${ROUTER}  Tenant: ${TENANT}"
echo

echo "→ Configure a tight day budget for this tenant via router env, or call router"
echo "  directly with X-Tenant-Id while TENANT_BUDGETS includes a low usd_day."
echo

post_via_router() {
  local content="$1"
  curl -sS "${ROUTER}/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -H "X-Tenant-Id: ${TENANT}" \
    -H "X-Cache-Bypass: 1" \
    -d "{\"model\":\"mock-small\",\"messages\":[{\"role\":\"user\",\"content\":$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "${content}")}]}"
}

echo "== Under-budget completions (router, cache bypass) =="
for i in 1 2; do
  echo "→ request ${i}"
  body="$(post_via_router "budget demo ping ${i}")"
  echo "${body}" | python3 -c "import json,sys; d=json.load(sys.stdin); print(f\"  provider={d.get('provider')} tokens={d.get('usage',{}).get('total_tokens')}\")"
done

echo
echo "== Usage summary =="
curl -sS "${ROUTER}/v1/tenants/${TENANT}/usage" | python3 -m json.tool

echo
echo "== Budget limits =="
curl -sS "${ROUTER}/v1/tenants/${TENANT}/budget" | python3 -m json.tool

echo
echo "== Gateway path (API key → tenant) =="
curl -sS "${GATEWAY}/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -H "X-Cache-Bypass: 1" \
  -d '{"model":"mock-small","messages":[{"role":"user","content":"gateway budget path"}]}' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(f\"provider={d.get('provider')} cached={d.get('cached')}\")"

echo
echo "Tip: set TENANT_BUDGETS='demo-budget:usd_day:0.003' on the router and re-run"
echo "until you see HTTP 402 budget_exceeded. Soft warnings use X-Budget-Warning: soft"
echo "when usage crosses BUDGET_SOFT_RATIO (default 0.8) of a hard limit."
