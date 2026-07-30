export type GatewayConfig = {
  port: number;
  routerUrl: string;
  /** API key → tenant id. Keys authenticate; tenant is always forwarded as X-Tenant-Id. */
  tenantApiKeys: Record<string, string>;
  logLevel: string;
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

export function loadConfig(env: NodeJS.ProcessEnv = process.env): GatewayConfig {
  const mapped = parseTenantApiKeys(env.TENANT_API_KEYS);
  const demoKey = env.DEMO_API_KEY ?? "demo-key-change-me";
  // Backward compatible: DEMO_API_KEY alone maps to tenant "default".
  const tenantApiKeys =
    Object.keys(mapped).length > 0 ? mapped : { [demoKey]: "default" };

  return {
    port: Number(env.PORT ?? "8080"),
    routerUrl: (env.ROUTER_URL ?? "http://127.0.0.1:8081").replace(/\/$/, ""),
    tenantApiKeys,
    logLevel: env.LOG_LEVEL ?? "info",
  };
}
