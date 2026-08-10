#!/usr/bin/env bash
# Demonstrate X-Request-Id echo + optional client-supplied correlation id.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GATEWAY_URL="${GATEWAY_URL:-http://localhost:18080}"
API_KEY="${DEMO_API_KEY:-demo-key-change-me}"
REQUEST_ID="${REQUEST_ID:-demo-$(date +%s)}"

echo "==> POST /v1/chat/completions with X-Request-Id: ${REQUEST_ID}"
HEADERS="$(mktemp)"
BODY="$(mktemp)"
trap 'rm -f "$HEADERS" "$BODY"' EXIT

curl -sS -D "$HEADERS" -o "$BODY" "${GATEWAY_URL}/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -H "X-Request-Id: ${REQUEST_ID}" \
  -d '{"model":"mock-small","messages":[{"role":"user","content":"trace demo"}]}'

echo "--- response headers (correlation) ---"
grep -iE '^(HTTP/|x-request-id:)' "$HEADERS" || true
echo "--- body ---"
if command -v jq >/dev/null 2>&1; then
  jq '{id, provider, cached, route_reason}' "$BODY"
else
  cat "$BODY"
  echo
fi

ECHOED="$(grep -i '^x-request-id:' "$HEADERS" | awk '{print $2}' | tr -d '\r')"
if [[ "$ECHOED" == "$REQUEST_ID" ]]; then
  echo "OK: X-Request-Id echoed (${ECHOED})"
else
  echo "WARN: expected X-Request-Id=${REQUEST_ID}, got '${ECHOED}'" >&2
  exit 1
fi

echo
echo "Tip: with monitoring profile + OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318,"
echo "open Jaeger UI (http://localhost:16686) and search service=gateway / router."
