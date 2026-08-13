import { timingSafeEqual } from "node:crypto";

/**
 * Resolve tenant for an API key using length-checked timing-safe compares.
 * Returns null when no key matches.
 */
export function resolveTenant(
  presented: string,
  tenantApiKeys: Record<string, string>,
): string | null {
  if (!presented || presented.length > 512) {
    return null;
  }
  const presentedBuf = Buffer.from(presented);
  let matched: string | null = null;
  for (const [key, tenant] of Object.entries(tenantApiKeys)) {
    const keyBuf = Buffer.from(key);
    if (keyBuf.length !== presentedBuf.length) {
      continue;
    }
    if (timingSafeEqual(keyBuf, presentedBuf)) {
      matched = tenant;
    }
  }
  return matched;
}

/** X-API-Key, or OpenAI-style `Authorization: Bearer <key>`. */
export function extractApiKey(
  headers: Record<string, unknown> | { [key: string]: unknown },
): string | undefined {
  const presented = headers["x-api-key"];
  if (typeof presented === "string" && presented.length > 0) {
    return presented;
  }
  const authorization = headers.authorization ?? headers["authorization"];
  if (typeof authorization !== "string") {
    return undefined;
  }
  const match = /^Bearer\s+(\S+)$/i.exec(authorization.trim());
  return match?.[1];
}
