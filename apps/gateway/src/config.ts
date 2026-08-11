export type GatewayConfig = {
  port: number;
  routerUrl: string;
  /** API key → tenant id. Keys authenticate; tenant is always forwarded as X-Tenant-Id. */
  tenantApiKeys: Record<string, string>;
  logLevel: string;
  /** Max JSON body size in bytes (auth + DoS hardening). */
  maxBodyBytes: number;
  /** OpenTelemetry tracing (spans always local; OTLP when endpoint set). */
  otelEnabled: boolean;
  otelServiceName: string;
  otelExporterOtlpEndpoint: string;
  otelConsoleExporter: boolean;
  /** Edge rate limiting (token bucket). Rate limit = concurrency; budget = spend. */
  rateLimitEnabled: boolean;
  rateLimitBackend: "redis" | "memory";
  redisUrl: string;
  rateLimitKeyQps: number;
  rateLimitKeyBurst: number;
  rateLimitTenantQps: number;
  rateLimitTenantBurst: number;
};

/** Parse `key:tenant,other:acme` (or JSON object) into a key→tenant map. */
export function parseTenantApiKeys(raw: string | undefined): Record<string, string> {
  if (!raw || !raw.trim()) {
    return {};
  }
  const trimmed = raw.trim();
  if (trimmed.startsWith("{")) {
    const parsed = JSON.parse(trimmed) as Record<string, string>;
    return Object.fromEntries(
      Object.entries(parsed).map(([k, v]) => [String(k), String(v)]),
    );
  }
  const out: Record<string, string> = {};
  for (const part of trimmed.split(",")) {
    const piece = part.trim();
    if (!piece) continue;
    const idx = piece.indexOf(":");
    if (idx <= 0) continue;
    const key = piece.slice(0, idx).trim();
    const tenant = piece.slice(idx + 1).trim();
    if (key && tenant) {
      out[key] = tenant;
    }
  }
  return out;
}

function parseBool(raw: string | undefined, defaultValue: boolean): boolean {
  if (raw === undefined || raw.trim() === "") {
    return defaultValue;
  }
  const v = raw.trim().toLowerCase();
  if (["1", "true", "yes", "on"].includes(v)) return true;
  if (["0", "false", "no", "off"].includes(v)) return false;
  return defaultValue;
}

function parseNonNegNumber(raw: string | undefined, defaultValue: number): number {
  if (raw === undefined || raw.trim() === "") {
    return defaultValue;
  }
  const n = Number(raw);
  if (!Number.isFinite(n) || n < 0) {
    return defaultValue;
  }
  return n;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): GatewayConfig {
  const mapped = parseTenantApiKeys(env.TENANT_API_KEYS);
  const demoKey = env.DEMO_API_KEY ?? "demo-key-change-me";
  // Backward compatible: DEMO_API_KEY alone maps to tenant "default".
  const tenantApiKeys =
    Object.keys(mapped).length > 0 ? mapped : { [demoKey]: "default" };

  const otelEnabledRaw = (env.OTEL_ENABLED ?? "true").trim().toLowerCase();
  const otelConsoleRaw = (env.OTEL_CONSOLE_EXPORTER ?? "false").trim().toLowerCase();

  const redisUrl = (
    env.RATE_LIMIT_REDIS_URL?.trim() ||
    env.REDIS_URL?.trim() ||
    "redis://127.0.0.1:6379/0"
  ).replace(/\/$/, "");

  const backendRaw = (env.RATE_LIMIT_BACKEND ?? "").trim().toLowerCase();
  const rateLimitBackend: "redis" | "memory" =
    backendRaw === "memory"
      ? "memory"
      : backendRaw === "redis"
        ? "redis"
        : env.REDIS_URL || env.RATE_LIMIT_REDIS_URL
          ? "redis"
          : "memory";

  const keyQps = parseNonNegNumber(env.RATE_LIMIT_KEY_QPS, 10);
  const keyBurst = parseNonNegNumber(
    env.RATE_LIMIT_KEY_BURST,
    keyQps > 0 ? keyQps * 2 : 20,
  );
  const tenantQps = parseNonNegNumber(env.RATE_LIMIT_TENANT_QPS, 0);
  const tenantBurst = parseNonNegNumber(
    env.RATE_LIMIT_TENANT_BURST,
    tenantQps > 0 ? tenantQps * 2 : 0,
  );

  return {
    port: Number(env.PORT ?? "8080"),
    routerUrl: (env.ROUTER_URL ?? "http://127.0.0.1:8081").replace(/\/$/, ""),
    tenantApiKeys,
    logLevel: env.LOG_LEVEL ?? "info",
    maxBodyBytes: Number(env.MAX_BODY_BYTES ?? String(256 * 1024)),
    otelEnabled: !["0", "false", "no", "off"].includes(otelEnabledRaw),
    otelServiceName: (env.OTEL_SERVICE_NAME ?? "gateway").trim() || "gateway",
    otelExporterOtlpEndpoint: (env.OTEL_EXPORTER_OTLP_ENDPOINT ?? "").trim().replace(/\/$/, ""),
    otelConsoleExporter: ["1", "true", "yes", "on"].includes(otelConsoleRaw),
    rateLimitEnabled: parseBool(env.RATE_LIMIT_ENABLED, true),
    rateLimitBackend,
    redisUrl,
    rateLimitKeyQps: keyQps,
    rateLimitKeyBurst: keyBurst,
    rateLimitTenantQps: tenantQps,
    rateLimitTenantBurst: tenantBurst,
  };
}
