/**
 * k6 snippet: hammer the gateway to observe rate-limit 429s.
 *
 * Requires a tight gateway limit, e.g.:
 *   RATE_LIMIT_KEY_QPS=2 RATE_LIMIT_KEY_BURST=3
 *
 *   k6 run load/rate_limit.js
 */
import http from "k6/http";
import { check } from "k6";
import { Counter } from "k6/metrics";

const BASE_URL = __ENV.BASE_URL || "http://localhost:18080";
const API_KEY = __ENV.API_KEY || "demo-key-change-me";

const okCount = new Counter("rl_ok");
const limitedCount = new Counter("rl_limited");

export const options = {
  vus: Number(__ENV.VUS || 8),
  duration: __ENV.DURATION || "10s",
};

export default function () {
  const res = http.post(
    `${BASE_URL}/v1/chat/completions`,
    JSON.stringify({
      model: "mock-small",
      messages: [{ role: "user", content: `rl ${__VU}-${__ITER}` }],
    }),
    {
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": API_KEY,
        "X-Cache-Bypass": "1",
      },
      tags: { name: "chat" },
    },
  );

  if (res.status === 200) {
    okCount.add(1);
  } else if (res.status === 429) {
    limitedCount.add(1);
  }

  check(res, {
    "status is 200 or 429": (r) => r.status === 200 || r.status === 429,
  });
}
