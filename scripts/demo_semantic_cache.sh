#!/usr/bin/env bash
# Demo: semantic cache hit rate + estimated USD saved.
# Requires the stack: docker compose up --build
set -euo pipefail

GATEWAY="${GATEWAY_URL:-http://localhost:18080}"
ROUTER="${ROUTER_URL_HOST:-http://localhost:18081}"
API_KEY="${DEMO_API_KEY:-demo-key-change-me}"
TENANT="${TENANT_ID:-demo-cache}"

post_chat() {
  local content="$1"
  python3 - "$GATEWAY" "$API_KEY" "$TENANT" "$content" <<'PY'
import json, os, sys, urllib.request

gateway, api_key, tenant, content = sys.argv[1:5]
req = urllib.request.Request(
    f"{gateway}/v1/chat/completions",
    data=json.dumps(
        {"model": "mock-small", "messages": [{"role": "user", "content": content}]}
    ).encode(),
    headers={
        "Content-Type": "application/json",
        "X-API-Key": api_key,
        "X-Tenant-Id": tenant,
    },
    method="POST",
)
with urllib.request.urlopen(req) as res:
    print(res.read().decode())
PY
}

echo "== Semantic cache demo =="
echo "Gateway: ${GATEWAY}  Tenant: ${TENANT}"
echo

PROMPTS=(
  "Explain semantic caching in one sentence."
  "Explain semantic caching in a single sentence."
  "Please explain semantic caching in one sentence"
  "What is the capital of France?"
)

hits=0
misses=0
for p in "${PROMPTS[@]}"; do
  echo "→ ${p}"
  body="$(post_chat "${p}")"
  eval "$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(f\"cached={d.get('cached')!r}; provider={d.get('provider')!r}; reason={d.get('route_reason')!r}\")" "${body}")"
  echo "  cached=${cached} provider=${provider} route_reason=${reason}"
  if [[ "${cached}" == "True" ]]; then
    hits=$((hits + 1))
  else
    misses=$((misses + 1))
  fi
  echo
done

echo "== Router cache stats =="
curl -sS "${ROUTER}/v1/cache/stats" | python3 -m json.tool

echo
echo "Local tally for this script: hits=${hits} misses=${misses}"
echo "Near-duplicates of the semantic-caching question should show cached=True."
echo "Unrelated prompts (e.g. capital of France) should miss and call the provider."
