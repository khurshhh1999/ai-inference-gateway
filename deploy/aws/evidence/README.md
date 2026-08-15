# Live verify evidence

`scripts/verify_live_bedrock.sh` writes timestamped JSON here after a Bedrock
verify. Those JSON files are **gitignored** — they may include request ids and
short content previews. Keep this README so the directory is tracked.

Committed sample shape (do not paste real secrets):

```json
{
  "verified_at_utc": "20260101T000000Z",
  "gateway": "https://<alb-dns>",
  "model": "gpt-proxy",
  "http_status": 200,
  "x_request_id": "live-bedrock-…",
  "provider": "bedrock",
  "route_reason": "failover",
  "cached": false,
  "id": "chatcmpl-bedrock-…",
  "content_preview": "bedrock-live-ok",
  "usage": { "prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16 }
}
```

See [`../LIVE.md`](../LIVE.md) for the full runbook.
