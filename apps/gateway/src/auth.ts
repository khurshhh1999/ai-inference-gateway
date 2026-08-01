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
