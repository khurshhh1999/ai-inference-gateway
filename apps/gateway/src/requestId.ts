import { randomUUID } from "node:crypto";

const MAX_LEN = 128;
const SAFE = /^[A-Za-z0-9._\-:+]{1,128}$/;

/** Accept a client id when safe; otherwise mint a new UUID. */
export function resolveRequestId(raw: unknown): string {
  if (typeof raw !== "string") {
    return randomUUID();
  }
  const trimmed = raw.trim();
  if (trimmed.length === 0 || trimmed.length > MAX_LEN || !SAFE.test(trimmed)) {
    return randomUUID();
  }
  return trimmed;
}
