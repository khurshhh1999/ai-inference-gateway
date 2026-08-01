/**
 * k6 load: baseline (cache bypass) vs cacheable mixed traffic.
 *
 * Usage (stack on :18080):
 *   k6 run load/chat.js
 *   k6 run -e SCENARIO=baseline load/chat.js
 *   k6 run -e SCENARIO=cached load/chat.js
 *   k6 run -e SCENARIO=compare load/chat.js   # default: both, prints delta
 */
import http from "k6/http";
import { check, sleep } from "k6";
import { Trend, Counter } from "k6/metrics";

const BASE_URL = __ENV.BASE_URL || "http://localhost:18080";
const API_KEY = __ENV.API_KEY || "demo-key-change-me";
const SCENARIO = (__ENV.SCENARIO || "compare").toLowerCase();
const VUS = Number(__ENV.VUS || 10);
const DURATION = __ENV.DURATION || "30s";

const baselineLatency = new Trend("baseline_latency_ms", true);
const cachedLatency = new Trend("cached_latency_ms", true);
const cacheHits = new Counter("load_cache_hits");
const cacheMisses = new Counter("load_cache_misses");

const sharedPrompt =
  "Explain semantic caching for LLM gateways in two short sentences.";

function chatPayload(prompt, stream = false) {
  return JSON.stringify({
    model: "mock-small",
    stream,
    messages: [{ role: "user", content: prompt }],
  });
}

function headers(bypass = false) {
  const h = {
    "Content-Type": "application/json",
    "X-API-Key": API_KEY,
  };
  if (bypass) {
    h["X-Cache-Bypass"] = "1";
  }
  return h;
}

export const options = (() => {
  if (SCENARIO === "baseline") {
    return {
      scenarios: {
        baseline: {
          executor: "constant-vus",
          vus: VUS,
          duration: DURATION,
          exec: "baseline",
        },
      },
      thresholds: {
        http_req_failed: ["rate<0.05"],
        baseline_latency_ms: ["p(95)<2000"],
      },
    };
  }
  if (SCENARIO === "cached") {
    return {
      scenarios: {
        warm: {
          executor: "shared-iterations",
          vus: 1,
          iterations: 3,
          exec: "warmCache",
          maxDuration: "30s",
        },
        cached: {
          executor: "constant-vus",
          vus: VUS,
          duration: DURATION,
          exec: "cached",
          startTime: "5s",
        },
      },
      thresholds: {
        http_req_failed: ["rate<0.05"],
        cached_latency_ms: ["p(95)<1000"],
      },
    };
  }
  // compare: run baseline then cached sequentially for a documented delta
  return {
    scenarios: {
      baseline: {
        executor: "constant-vus",
        vus: VUS,
        duration: DURATION,
        exec: "baseline",
      },
      warm: {
        executor: "shared-iterations",
        vus: 1,
        iterations: 5,
        exec: "warmCache",
        startTime: DURATION,
        maxDuration: "30s",
      },
      cached: {
        executor: "constant-vus",
        vus: VUS,
        duration: DURATION,
        exec: "cached",
        startTime: addDuration(DURATION, "8s"),
      },
    },
    thresholds: {
      http_req_failed: ["rate<0.05"],
    },
  };
})();

function addDuration(a, b) {
  // naive "30s" + "8s" → "38s" for startTime chaining
  const parse = (s) => {
    const m = String(s).match(/^(\d+)(s|m)$/);
    if (!m) return 0;
    return m[2] === "m" ? Number(m[1]) * 60 : Number(m[1]);
  };
  return `${parse(a) + parse(b)}s`;
}

function postChat(prompt, bypass) {
  const res = http.post(`${BASE_URL}/v1/chat/completions`, chatPayload(prompt), {
    headers: headers(bypass),
    tags: { bypass: String(bypass) },
  });
  check(res, {
    "status 200": (r) => r.status === 200,
  });
  let cached = false;
  try {
    const body = res.json();
    cached = Boolean(body && body.cached);
  } catch (_) {
    // ignore parse errors
  }
  if (cached) cacheHits.add(1);
  else cacheMisses.add(1);
  return res;
}

export function warmCache() {
  postChat(sharedPrompt, false);
  sleep(0.1);
}

export function baseline() {
  // Unique-ish prompts + bypass → always miss (provider path)
  const prompt = `${sharedPrompt} request=${__VU}-${__ITER}-${Date.now()}`;
  const res = postChat(prompt, true);
  baselineLatency.add(res.timings.duration);
  sleep(0.05);
}

export function cached() {
  // Repeated prompt → semantic cache hit after warm
  const res = postChat(sharedPrompt, false);
  cachedLatency.add(res.timings.duration);
  sleep(0.05);
}

export function handleSummary(data) {
  const baseP95 = data.metrics.baseline_latency_ms?.values?.["p(95)"];
  const cacheP95 = data.metrics.cached_latency_ms?.values?.["p(95)"];
  let improvement = null;
  if (baseP95 && cacheP95 && baseP95 > 0) {
    improvement = ((baseP95 - cacheP95) / baseP95) * 100;
  }

  const lines = [
    "# k6 load summary",
    "",
    `- Scenario: ${SCENARIO}`,
    `- Base URL: ${BASE_URL}`,
    `- VUs: ${VUS}, duration/phase: ${DURATION}`,
    baseP95 !== undefined
      ? `- Baseline p95 (cache bypass): ${baseP95.toFixed(2)} ms`
      : "- Baseline p95: n/a",
    cacheP95 !== undefined
      ? `- Cached p95 (repeated prompts): ${cacheP95.toFixed(2)} ms`
      : "- Cached p95: n/a",
    improvement !== null
      ? `- p95 improvement: ${improvement.toFixed(1)}% (target ≥35%)`
      : "- p95 improvement: n/a (run SCENARIO=compare)",
    "",
  ];

  return {
    stdout: lines.join("\n"),
    "load/summary.json": JSON.stringify(
      {
        scenario: SCENARIO,
        baseline_p95_ms: baseP95 ?? null,
        cached_p95_ms: cacheP95 ?? null,
        p95_improvement_pct: improvement,
        http_reqs: data.metrics.http_reqs?.values?.count ?? null,
        http_req_failed: data.metrics.http_req_failed?.values?.rate ?? null,
      },
      null,
      2,
    ),
  };
}
