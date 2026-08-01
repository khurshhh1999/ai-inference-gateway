# Load test results

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
| Captured (UTC) | 2026-08-01 15:42:37Z |
| Baseline p95 (cache bypass / unique prompts) | 71.50 ms |
| Cached p95 (repeated prompts) | 28.45 ms |
| p95 improvement | 60.21% |
| Target | ≥ 35% |
| Meets target | yes |
| HTTP requests | 3007 |
| HTTP failure rate | 0 |

## Interpretation

- **Baseline** forces `X-Cache-Bypass: 1` and unique prompts so every request
  hits the mock provider (`MOCK_LATENCY_MS`, default 40ms) plus gateway/router
  overhead.
- **Cached** warms one prompt, then repeats it so semantic cache hits dominate.
  Hit path skips the provider, which is where the p95 win comes from on
  cacheable traffic mixes.

Re-run after changing cache thresholds, mock latency, or hardware and commit an
updated `load/RESULTS.md` when numbers change materially.
