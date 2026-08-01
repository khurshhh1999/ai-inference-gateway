#!/usr/bin/env bash
# Run the compare load scenario and refresh RESULTS.md from summary.json.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

BASE_URL="${BASE_URL:-http://localhost:18080}"
API_KEY="${API_KEY:-demo-key-change-me}"
VUS="${VUS:-10}"
DURATION="${DURATION:-30s}"

echo "Checking gateway health at ${BASE_URL}..."
curl -sf "${BASE_URL}/health" >/dev/null

echo "Running k6 compare (baseline then cached)..."
k6 run \
  -e "BASE_URL=${BASE_URL}" \
  -e "API_KEY=${API_KEY}" \
  -e "VUS=${VUS}" \
  -e "DURATION=${DURATION}" \
  -e "SCENARIO=compare" \
  load/chat.js

python3 - <<'PY'
import json
from datetime import datetime, timezone
from pathlib import Path

summary_path = Path("load/summary.json")
results_path = Path("load/RESULTS.md")
data = json.loads(summary_path.read_text())
base = data.get("baseline_p95_ms")
cached = data.get("cached_p95_ms")
imp = data.get("p95_improvement_pct")
ok = imp is not None and imp >= 35.0

def fmt(v):
    return "n/a" if v is None else f"{v:.2f}"

body = f"""# Load test results

Measured with [k6](https://k6.io) against the local mock stack
(`PROVIDER_MODE=mock`, semantic cache enabled).

## Command

```bash
./load/run.sh
# or: k6 run -e SCENARIO=compare load/chat.js
```

## Latest compare run

| Metric | Value |
|--------|------:|
| Captured (UTC) | {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")} |
| Baseline p95 (cache bypass / unique prompts) | {fmt(base)} ms |
| Cached p95 (repeated prompts) | {fmt(cached)} ms |
| p95 improvement | {fmt(imp)}% |
| Target | ≥ 35% |
| Meets target | {"yes" if ok else "no / n/a"} |
| HTTP requests | {data.get("http_reqs")} |
| HTTP failure rate | {data.get("http_req_failed")} |

## Interpretation

- **Baseline** forces `X-Cache-Bypass: 1` and unique prompts so every request
  hits the mock provider (`MOCK_LATENCY_MS`, default 40ms) plus gateway/router
  overhead.
- **Cached** warms one prompt, then repeats it so semantic cache hits dominate.
  Hit path skips the provider, which is where the p95 win comes from on
  cacheable traffic mixes.

Re-run after changing cache thresholds, mock latency, or hardware and commit an
updated `load/RESULTS.md` when numbers change materially.
"""
results_path.write_text(body)
print(f"Wrote {results_path}")
PY
