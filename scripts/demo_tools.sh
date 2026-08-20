#!/usr/bin/env bash
# Demo: OpenAI-shaped tool / function calling through the gateway (mock provider).
# Requires the stack: docker compose up --build
set -euo pipefail

GATEWAY="${GATEWAY_URL:-http://localhost:18080}"
API_KEY="${DEMO_API_KEY:-demo-key-change-me}"

echo "== Tool call (auto: function name in the user message) =="
FIRST="$(curl -sS "${GATEWAY}/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -H "X-Request-Id: demo-tools-001" \
  -d '{
    "model": "mock-small",
    "messages": [{"role": "user", "content": "What is the weather in Boston? Use get_weather."}],
    "tools": [{
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "Look up weather for a city",
        "parameters": {
          "type": "object",
          "properties": {"location": {"type": "string"}},
          "required": ["location"]
        }
      }
    }]
  }')"

echo "$FIRST" | python3 -c "
import json, sys
d = json.load(sys.stdin)
msg = d['choices'][0]['message']
print('finish_reason=', d['choices'][0]['finish_reason'])
print('provider=', d.get('provider'), 'cached=', d.get('cached'))
print('content=', msg.get('content'))
calls = msg.get('tool_calls') or []
for c in calls:
    print('tool=', c['id'], c['function']['name'], c['function']['arguments'])
if d['choices'][0]['finish_reason'] != 'tool_calls' or not calls:
    raise SystemExit('expected a tool_calls response from the mock provider')
"

CALL_JSON="$(echo "$FIRST" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(json.dumps(d['choices'][0]['message']['tool_calls'][0]))
")"

echo
echo "== Tool result follow-up (final assistant text) =="
FOLLOWUP="$(CALL_JSON="$CALL_JSON" python3 -c "
import json, os
call = json.loads(os.environ['CALL_JSON'])
print(json.dumps({
  'model': 'mock-small',
  'messages': [
    {'role': 'user', 'content': 'What is the weather in Boston? Use get_weather.'},
    {'role': 'assistant', 'content': None, 'tool_calls': [call]},
    {'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps({'temp_f': 72, 'conditions': 'sunny'})},
  ],
  'tools': [{
    'type': 'function',
    'function': {
      'name': 'get_weather',
      'description': 'Look up weather for a city',
      'parameters': {
        'type': 'object',
        'properties': {'location': {'type': 'string'}},
        'required': ['location']
      }
    }
  }]
}))
")"

curl -sS "${GATEWAY}/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "X-Request-Id: demo-tools-002" \
  -d "$FOLLOWUP" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print('finish_reason=', d['choices'][0]['finish_reason'])
print('content=', d['choices'][0]['message']['content'])
"

echo
echo "== Streaming tool call (single tool_calls chunk, then [DONE]) =="
curl -N -sS "${GATEWAY}/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -d '{
    "model": "mock-small",
    "stream": true,
    "messages": [{"role": "user", "content": "Call get_weather for Paris"}],
    "tools": [{
      "type": "function",
      "function": {"name": "get_weather", "parameters": {"type": "object", "properties": {"location": {"type": "string"}}, "required": ["location"]}}
    }]
  }'
echo
