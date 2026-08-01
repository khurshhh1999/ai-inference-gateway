import client from "prom-client";

const register = new client.Registry();
client.collectDefaultMetrics({ register, prefix: "gateway_" });

const latencyBuckets = [
  0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
];

export const requestDuration = new client.Histogram({
  name: "gateway_request_duration_seconds",
  help: "Gateway HTTP request latency",
  labelNames: ["method", "route", "status"] as const,
  buckets: latencyBuckets,
  registers: [register],
});

export const authFailures = new client.Counter({
  name: "gateway_auth_failures_total",
  help: "Rejected requests due to missing/invalid API key",
  registers: [register],
});

export const upstreamErrors = new client.Counter({
  name: "gateway_upstream_errors_total",
  help: "Upstream router failures (unreachable or 5xx)",
  labelNames: ["kind"] as const,
  registers: [register],
});

export const bodyRejected = new client.Counter({
  name: "gateway_body_rejected_total",
  help: "Requests rejected for oversized or invalid body",
  labelNames: ["reason"] as const,
  registers: [register],
});

export async function metricsPayload(): Promise<{
  body: string;
  contentType: string;
}> {
  return {
    body: await register.metrics(),
    contentType: register.contentType,
  };
}

export function resetMetricsForTests(): void {
  register.resetMetrics();
}
