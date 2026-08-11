import { createHash } from "node:crypto";
import { createClient, type RedisClientType } from "redis";

export type RateLimitScope = "key" | "tenant";

export type RateLimitDecision = {
  allowed: boolean;
  limit: number;
  remaining: number;
  retryAfterMs: number;
  scope: RateLimitScope;
};

export type RateLimiter = {
  /** Consume one token from the bucket identified by scope+id. */
  take(
    scope: RateLimitScope,
    id: string,
    qps: number,
    burst: number,
  ): Promise<RateLimitDecision>;
  close(): Promise<void>;
};

/** Stable short id for Redis keys — never store raw API keys. */
export function hashApiKey(apiKey: string): string {
  return createHash("sha256").update(apiKey).digest("hex").slice(0, 24);
}

const TOKEN_BUCKET_LUA = `
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now_ms = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])

local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])

if tokens == nil then
  tokens = capacity
  ts = now_ms
end

local elapsed = math.max(0, now_ms - ts) / 1000.0
tokens = math.min(capacity, tokens + elapsed * rate)
ts = now_ms

local allowed = 0
local retry_after = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  if rate > 0 then
    retry_after = math.ceil((cost - tokens) / rate * 1000)
  else
    retry_after = 1000
  end
end

redis.call('HMSET', key, 'tokens', tokens, 'ts', ts)
local ttl_ms = math.ceil((capacity / math.max(rate, 0.001)) * 2000) + 1000
redis.call('PEXPIRE', key, ttl_ms)

return {allowed, math.floor(tokens), retry_after}
`;

type BucketState = { tokens: number; ts: number };

function applyBucket(
  state: BucketState | undefined,
  capacity: number,
  rate: number,
  nowMs: number,
  cost: number,
): { next: BucketState; allowed: boolean; remaining: number; retryAfterMs: number } {
  let tokens = state?.tokens ?? capacity;
  let ts = state?.ts ?? nowMs;
  const elapsed = Math.max(0, nowMs - ts) / 1000;
  tokens = Math.min(capacity, tokens + elapsed * rate);
  ts = nowMs;

  if (tokens >= cost) {
    tokens -= cost;
    return {
      next: { tokens, ts },
      allowed: true,
      remaining: Math.floor(tokens),
      retryAfterMs: 0,
    };
  }
  const retryAfterMs =
    rate > 0 ? Math.ceil(((cost - tokens) / rate) * 1000) : 1000;
  return {
    next: { tokens, ts },
    allowed: false,
    remaining: Math.floor(tokens),
    retryAfterMs,
  };
}

/** Always allows — used when rate limiting is disabled. */
export class NoopRateLimiter implements RateLimiter {
  async take(
    scope: RateLimitScope,
    _id: string,
    qps: number,
    burst: number,
  ): Promise<RateLimitDecision> {
    const limit = Math.max(1, Math.floor(burst > 0 ? burst : qps));
    return {
      allowed: true,
      limit,
      remaining: limit,
      retryAfterMs: 0,
      scope,
    };
  }

  async close(): Promise<void> {}
}

/** In-process token bucket (unit tests / local without Redis). */
export class MemoryRateLimiter implements RateLimiter {
  private readonly buckets = new Map<string, BucketState>();

  async take(
    scope: RateLimitScope,
    id: string,
    qps: number,
    burst: number,
  ): Promise<RateLimitDecision> {
    const rate = Math.max(0, qps);
    const capacity = Math.max(1, Math.floor(burst > 0 ? burst : Math.max(rate, 1)));
    const key = `${scope}:${id}`;
    const nowMs = Date.now();
    const result = applyBucket(this.buckets.get(key), capacity, rate, nowMs, 1);
    this.buckets.set(key, result.next);
    return {
      allowed: result.allowed,
      limit: capacity,
      remaining: result.remaining,
      retryAfterMs: result.retryAfterMs,
      scope,
    };
  }

  async close(): Promise<void> {
    this.buckets.clear();
  }
}

/**
 * Redis token-bucket limiter. On Redis errors, fails open (allows the request)
 * so a cache outage does not take down the edge — callers should count errors.
 */
export class RedisRateLimiter implements RateLimiter {
  private client: RedisClientType | null = null;
  private connecting: Promise<RedisClientType> | null = null;
  private onError: ((err: unknown) => void) | null = null;

  constructor(
    private readonly redisUrl: string,
    opts?: { onError?: (err: unknown) => void },
  ) {
    this.onError = opts?.onError ?? null;
  }

  private async getClient(): Promise<RedisClientType> {
    if (this.client?.isOpen) {
      return this.client;
    }
    if (this.connecting) {
      return this.connecting;
    }
    this.connecting = (async () => {
      const client = createClient({ url: this.redisUrl }) as RedisClientType;
      client.on("error", (err) => {
        this.onError?.(err);
      });
      await client.connect();
      this.client = client;
      return client;
    })();
    try {
      return await this.connecting;
    } finally {
      this.connecting = null;
    }
  }

  async take(
    scope: RateLimitScope,
    id: string,
    qps: number,
    burst: number,
  ): Promise<RateLimitDecision> {
    const rate = Math.max(0, qps);
    const capacity = Math.max(1, Math.floor(burst > 0 ? burst : Math.max(rate, 1)));
    const redisKey = `rl:${scope}:${id}`;

    try {
      const client = await this.getClient();
      const raw = (await client.eval(TOKEN_BUCKET_LUA, {
        keys: [redisKey],
        arguments: [
          String(capacity),
          String(rate),
          String(Date.now()),
          "1",
        ],
      })) as [number, number, number];

      return {
        allowed: Number(raw[0]) === 1,
        limit: capacity,
        remaining: Math.max(0, Number(raw[1])),
        retryAfterMs: Math.max(0, Number(raw[2])),
        scope,
      };
    } catch (err) {
      this.onError?.(err);
      // Fail open — edge stays available if Redis is briefly unreachable.
      return {
        allowed: true,
        limit: capacity,
        remaining: capacity,
        retryAfterMs: 0,
        scope,
      };
    }
  }

  async close(): Promise<void> {
    if (this.client?.isOpen) {
      await this.client.quit();
    }
    this.client = null;
  }
}

export type RateLimitConfig = {
  enabled: boolean;
  backend: "redis" | "memory";
  redisUrl: string;
  keyQps: number;
  keyBurst: number;
  tenantQps: number;
  tenantBurst: number;
};

export function createRateLimiter(
  config: RateLimitConfig,
  opts?: { onRedisError?: (err: unknown) => void },
): RateLimiter {
  if (!config.enabled) {
    return new NoopRateLimiter();
  }
  if (config.backend === "memory") {
    return new MemoryRateLimiter();
  }
  return new RedisRateLimiter(config.redisUrl, { onError: opts?.onRedisError });
}

/**
 * Run configured key + tenant checks. Returns the first rejection, or the
 * tightest (lowest remaining) allow decision for response headers.
 */
export async function enforceRateLimits(
  limiter: RateLimiter,
  config: RateLimitConfig,
  apiKeyHash: string,
  tenantId: string,
): Promise<RateLimitDecision | null> {
  if (!config.enabled) {
    return null;
  }

  const checks: Array<{
    scope: RateLimitScope;
    id: string;
    qps: number;
    burst: number;
  }> = [];

  if (config.keyQps > 0) {
    checks.push({
      scope: "key",
      id: apiKeyHash,
      qps: config.keyQps,
      burst: config.keyBurst > 0 ? config.keyBurst : config.keyQps * 2,
    });
  }
  if (config.tenantQps > 0) {
    checks.push({
      scope: "tenant",
      id: tenantId,
      qps: config.tenantQps,
      burst: config.tenantBurst > 0 ? config.tenantBurst : config.tenantQps * 2,
    });
  }
  if (checks.length === 0) {
    return null;
  }

  let tightest: RateLimitDecision | null = null;
  for (const check of checks) {
    const decision = await limiter.take(
      check.scope,
      check.id,
      check.qps,
      check.burst,
    );
    if (!decision.allowed) {
      return decision;
    }
    if (!tightest || decision.remaining < tightest.remaining) {
      tightest = decision;
    }
  }
  return tightest;
}
