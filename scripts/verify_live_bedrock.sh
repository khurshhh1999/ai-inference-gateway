#!/usr/bin/env bash
# Verify a live Bedrock path through the gateway.
#
# Success criteria (not mock):
#   - HTTP 200
#   - provider == "bedrock"
#   - cached is not true
#   - id starts with chatcmpl-bedrock-
#   - message content is not the mock echo pattern
#
# Local dry-run (Compose overlay):
#   docker compose -f docker-compose.yml -f docker-compose.bedrock.yml up --build -d
#   ./scripts/verify_live_bedrock.sh
#
# Against a deployed ALB / public gateway:
#   GATEWAY_URL=https://<alb-dns> DEMO_API_KEY=... ./scripts/verify_live_bedrock.sh
#
# Writes a redacted evidence file under deploy/aws/evidence/.
set -euo pipefail

GATEWAY="${GATEWAY_URL:-http://localhost:18080}"
API_KEY="${DEMO_API_KEY:-demo-key-change-me}"
MODEL="${LIVE_MODEL:-gpt-proxy}"
OUT_DIR="${EVIDENCE_DIR:-deploy/aws/evidence}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BODY="$(mktemp)"
HDRS="$(mktemp)"
trap 'rm -f "$BODY" "$HDRS"' EXIT

echo "== Live Bedrock verify =="
echo "Gateway: ${GATEWAY}"
echo "Model:   ${MODEL}"
echo

code="$(curl -sS -D "$HDRS" -o "$BODY" -w '%{http_code}' \
  "${GATEWAY}/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -H "X-Cache-Bypass: 1" \
  -H "X-Request-Id: live-bedrock-${STAMP}" \
  -d "{\"model\":\"${MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: bedrock-live-ok\"}],\"max_tokens\":32}")"

req_id="$(awk -F': ' 'tolower($1)=="x-request-id"{gsub(/\r/,"",$2); print $2; exit}' "$HDRS" || true)"

echo "HTTP ${code}"
echo "X-Request-Id: ${req_id:-"(none)"}"
echo

if [[ "${code}" != "200" ]]; then
  echo "FAIL: expected HTTP 200 from a live Bedrock call"
  echo "Body:"
  cat "$BODY"
  echo
  echo "Hints:"
  echo "  - Use the Bedrock compose overlay (docker-compose.bedrock.yml)"
  echo "  - Confirm AWS credentials + Bedrock model access in AWS_REGION"
  echo "  - Router image must be built with INSTALL_EXTRAS='[bedrock]'"
  exit 1
fi

# shellcheck disable=SC2046
eval $(python3 -c "
import json, re, sys
d = json.loads(open(sys.argv[1]).read())
provider = d.get('provider')
cached = d.get('cached')
reason = d.get('route_reason')
cid = d.get('id') or ''
content = ''
try:
    content = d['choices'][0]['message']['content'] or ''
except Exception:
    content = ''
mockish = '1' if re.search(r'\\[mock:.+\\]\\s*Echo', content) else '0'
def q(v):
    if v is None:
        return ''
    return str(v).replace(\"'\", \"'\\\\''\")
print(f\"provider='{q(provider)}'\")
print(f\"cached='{q(cached)}'\")
print(f\"reason='{q(reason)}'\")
print(f\"cid='{q(cid)}'\")
print(f\"mockish='{mockish}'\")
print(f\"content_preview='{q(content[:120])}'\")
" "$BODY")

echo "provider=${provider} route_reason=${reason} cached=${cached}"
echo "id=${cid}"
echo "content_preview=${content_preview}"
echo

ok=1
if [[ "${provider}" != "bedrock" ]]; then
  echo "FAIL: provider must be bedrock (got ${provider})"
  ok=0
fi
if [[ "${cached}" == "True" || "${cached}" == "true" ]]; then
  echo "FAIL: response was cached; bypass cache for a live proof"
  ok=0
fi
if [[ "${cid}" != chatcmpl-bedrock-* ]]; then
  echo "FAIL: id must start with chatcmpl-bedrock- (got ${cid})"
  ok=0
fi
if [[ "${mockish}" == "1" ]]; then
  echo "FAIL: content looks like the mock echo provider"
  ok=0
fi

mkdir -p "$OUT_DIR"
EVIDENCE="${OUT_DIR}/bedrock-verify-${STAMP}.json"
python3 -c "
import json
from pathlib import Path
raw = json.loads(Path('${BODY}').read_text())
content = ''
try:
    content = raw['choices'][0]['message']['content'] or ''
except Exception:
    pass
evidence = {
    'verified_at_utc': '${STAMP}',
    'gateway': '${GATEWAY}',
    'model': '${MODEL}',
    'http_status': int('${code}'),
    'x_request_id': '''${req_id}''' or None,
    'provider': raw.get('provider'),
    'route_reason': raw.get('route_reason'),
    'cached': raw.get('cached'),
    'id': raw.get('id'),
    'content_preview': content[:160],
    'usage': raw.get('usage'),
}
path = Path('${EVIDENCE}')
path.write_text(json.dumps(evidence, indent=2) + '\n')
print(f'Wrote evidence: {path}')
"

if [[ "${ok}" != "1" ]]; then
  exit 1
fi

echo
echo "PASS: live Bedrock response confirmed (provider=bedrock)."
echo "Attach ${EVIDENCE} (or a redacted copy) when documenting a cloud deploy."
