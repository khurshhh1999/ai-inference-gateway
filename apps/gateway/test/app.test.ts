import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";
import type { FastifyInstance } from "fastify";
import { buildServer } from "../src/app.js";
import { loadConfig, parseTenantApiKeys } from "../src/config.js";
import { extractApiKey } from "../src/auth.js";
import { MemoryRateLimiter } from "../src/rateLimit.js";
import { resetMetricsForTests } from "../src/metrics.js";

describe("gateway", () => {
  let app: FastifyInstance;
  const originalFetch = globalThis.fetch;

  before(async () => {
    app = await buildServer(
      loadConfig({
        PORT: "0",
        ROUTER_URL: "http://router.test",
        DEMO_API_KEY: "demo-key-change-me",
        LOG_LEVEL: "silent",
        // Keep shared suite independent of Redis / rate limits.
        RATE_LIMIT_ENABLED: "false",
      }),
    );
    await app.ready();
  });

  after(async () => {
    globalThis.fetch = originalFetch;
    await app.close();
  });

  it("health does not require API key", async () => {
    const res = await app.inject({ method: "GET", url: "/health" });
    assert.equal(res.statusCode, 200);
    assert.equal(res.json().service, "gateway");
  });

  it("exposes prometheus metrics without API key", async () => {
    const res = await app.inject({ method: "GET", url: "/metrics" });
    assert.equal(res.statusCode, 200);
    assert.match(res.body, /gateway_request_duration_seconds/);
  });

  it("serves OpenAPI document", async () => {
    const res = await app.inject({ method: "GET", url: "/openapi.json" });
    assert.equal(res.statusCode, 200);
    const doc = res.json() as { info: { title: string }; paths: Record<string, unknown> };
    assert.equal(doc.info.title, "AI Inference Gateway");
    assert.ok(doc.paths["/v1/chat/completions"]);
    assert.ok(doc.paths["/v1/models"]);
    assert.ok(doc.paths["/v1/embeddings"]);
  });

  it("rejects missing API key", async () => {
    const res = await app.inject({
      method: "POST",
      url: "/v1/chat/completions",
      payload: { model: "mock-small", messages: [{ role: "user", content: "hi" }] },
    });
    assert.equal(res.statusCode, 401);
  });

  it("rejects oversized request bodies", async () => {
    const limited = await buildServer(
      loadConfig({
        PORT: "0",
        ROUTER_URL: "http://router.test",
        DEMO_API_KEY: "demo-key-change-me",
        LOG_LEVEL: "silent",
        MAX_BODY_BYTES: "200",
        RATE_LIMIT_ENABLED: "false",
      }),
    );
    await limited.ready();
    try {
      const res = await limited.inject({
        method: "POST",
        url: "/v1/chat/completions",
        headers: { "x-api-key": "demo-key-change-me" },
        payload: {
          model: "mock-small",
          messages: [{ role: "user", content: "x".repeat(500) }],
        },
      });
      assert.equal(res.statusCode, 413);
    } finally {
      await limited.close();
    }
  });

  it("proxies completions to the router", async () => {
    globalThis.fetch = (async () =>
      new Response(
        JSON.stringify({
          id: "chatcmpl-test",
          object: "chat.completion",
          created: 1,
          model: "mock-small",
          choices: [
            {
              index: 0,
              message: { role: "assistant", content: "hello" },
              finish_reason: "stop",
            },
          ],
          usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
          provider: "mock",
          cached: false,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      )) as typeof fetch;

    const res = await app.inject({
      method: "POST",
      url: "/v1/chat/completions",
      headers: { "x-api-key": "demo-key-change-me" },
      payload: {
        model: "mock-small",
        messages: [{ role: "user", content: "hi" }],
      },
    });

    assert.equal(res.statusCode, 200);
    assert.equal(res.json().provider, "mock");
    assert.equal(res.json().choices[0].message.content, "hello");
  });

  it("forwards tools and tool_choice to the router", async () => {
    let sawBody: {
      tools?: unknown[];
      tool_choice?: unknown;
      messages?: Array<{ role: string }>;
    } | null = null;
    globalThis.fetch = (async (_input, init) => {
      sawBody = JSON.parse(String(init?.body ?? "{}")) as typeof sawBody;
      return new Response(
        JSON.stringify({
          id: "c1",
          object: "chat.completion",
          created: 1,
          model: "mock-small",
          choices: [
            {
              index: 0,
              message: {
                role: "assistant",
                content: null,
                tool_calls: [
                  {
                    id: "call_1",
                    type: "function",
                    function: { name: "get_weather", arguments: '{"location":"Boston"}' },
                  },
                ],
              },
              finish_reason: "tool_calls",
            },
          ],
          usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
          provider: "mock",
          cached: false,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }) as typeof fetch;

    const res = await app.inject({
      method: "POST",
      url: "/v1/chat/completions",
      headers: { "x-api-key": "demo-key-change-me" },
      payload: {
        model: "mock-small",
        messages: [{ role: "user", content: "weather in Boston" }],
        tools: [
          {
            type: "function",
            function: { name: "get_weather", description: "weather lookup" },
          },
        ],
        tool_choice: "auto",
      },
    });

    assert.equal(res.statusCode, 200);
    assert.equal(res.json().choices[0].finish_reason, "tool_calls");
    assert.ok(sawBody?.tools);
    assert.equal(sawBody?.tool_choice, "auto");
  });

  it("forwards tenant from API key map (ignores client X-Tenant-Id)", async () => {
    let sawTenant: string | null = null;
    globalThis.fetch = (async (_input, init) => {
      const headers = init?.headers as Record<string, string>;
      sawTenant = headers["x-tenant-id"] ?? null;
      return new Response(
        JSON.stringify({
          id: "c1",
          object: "chat.completion",
          created: 1,
          model: "mock-small",
          choices: [
            {
              index: 0,
              message: { role: "assistant", content: "ok" },
              finish_reason: "stop",
            },
          ],
          usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
          provider: "mock",
          cached: false,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }) as typeof fetch;

    const keyed = await buildServer(
      loadConfig({
        PORT: "0",
        ROUTER_URL: "http://router.test",
        TENANT_API_KEYS: "acme-key:acme,demo-key-change-me:default",
        LOG_LEVEL: "silent",
        RATE_LIMIT_ENABLED: "false",
      }),
    );
    await keyed.ready();
    try {
      const res = await keyed.inject({
        method: "POST",
        url: "/v1/chat/completions",
        headers: {
          "x-api-key": "acme-key",
          "x-tenant-id": "spoofed",
        },
        payload: {
          model: "mock-small",
          messages: [{ role: "user", content: "hi" }],
        },
      });
      assert.equal(res.statusCode, 200);
      assert.equal(sawTenant, "acme");
    } finally {
      await keyed.close();
    }
  });

  it("proxies 402 budget errors from the router", async () => {
    globalThis.fetch = (async () =>
      new Response(
        JSON.stringify({
          detail: {
            error: "budget_exceeded",
            message: "Tenant 'acme' exceeded usd day budget",
            tenant: "acme",
            window: "day",
            metric: "usd",
            used: 1,
            limit: 1,
          },
        }),
        { status: 402, headers: { "content-type": "application/json" } },
      )) as typeof fetch;

    const res = await app.inject({
      method: "POST",
      url: "/v1/chat/completions",
      headers: { "x-api-key": "demo-key-change-me" },
      payload: {
        model: "mock-small",
        messages: [{ role: "user", content: "hi" }],
      },
    });
    assert.equal(res.statusCode, 402);
    assert.equal(res.json().detail.error, "budget_exceeded");
  });

  it("echoes client X-Request-Id and forwards it upstream", async () => {
    let sawRequestId: string | null = null;
    let sawTraceparent = false;
    globalThis.fetch = (async (_input, init) => {
      const headers = init?.headers as Record<string, string>;
      sawRequestId = headers["x-request-id"] ?? null;
      sawTraceparent = typeof headers["traceparent"] === "string";
      return new Response(
        JSON.stringify({
          id: "c1",
          object: "chat.completion",
          created: 1,
          model: "mock-small",
          choices: [
            {
              index: 0,
              message: { role: "assistant", content: "ok" },
              finish_reason: "stop",
            },
          ],
          usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
          provider: "mock",
          cached: false,
        }),
        {
          status: 200,
          headers: {
            "content-type": "application/json",
            "x-request-id": headers["x-request-id"] ?? "",
          },
        },
      );
    }) as typeof fetch;

    const res = await app.inject({
      method: "POST",
      url: "/v1/chat/completions",
      headers: {
        "x-api-key": "demo-key-change-me",
        "x-request-id": "client-req-abc-001",
      },
      payload: {
        model: "mock-small",
        messages: [{ role: "user", content: "hi" }],
      },
    });

    assert.equal(res.statusCode, 200);
    assert.equal(res.headers["x-request-id"], "client-req-abc-001");
    assert.equal(sawRequestId, "client-req-abc-001");
    assert.equal(sawTraceparent, true);
  });

  it("generates X-Request-Id when missing", async () => {
    globalThis.fetch = (async (_input, init) => {
      const headers = init?.headers as Record<string, string>;
      return new Response(
        JSON.stringify({
          id: "c1",
          object: "chat.completion",
          created: 1,
          model: "mock-small",
          choices: [
            {
              index: 0,
              message: { role: "assistant", content: "ok" },
              finish_reason: "stop",
            },
          ],
          usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
          provider: "mock",
          cached: false,
        }),
        {
          status: 200,
          headers: {
            "content-type": "application/json",
            "x-request-id": headers["x-request-id"] ?? "",
          },
        },
      );
    }) as typeof fetch;

    const res = await app.inject({
      method: "POST",
      url: "/v1/chat/completions",
      headers: { "x-api-key": "demo-key-change-me" },
      payload: {
        model: "mock-small",
        messages: [{ role: "user", content: "hi" }],
      },
    });

    assert.equal(res.statusCode, 200);
    assert.match(String(res.headers["x-request-id"] ?? ""), /^[0-9a-f-]{36}$/i);
  });

  it("accepts Authorization Bearer as an API key alias", async () => {
    let sawTenant: string | null = null;
    globalThis.fetch = (async (_input, init) => {
      const headers = init?.headers as Record<string, string>;
      sawTenant = headers["x-tenant-id"] ?? null;
      return new Response(
        JSON.stringify({
          object: "list",
          data: [{ id: "mock-small", object: "model", created: 1, owned_by: "mock", purpose: "chat" }],
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }) as typeof fetch;

    const res = await app.inject({
      method: "GET",
      url: "/v1/models",
      headers: { authorization: "Bearer demo-key-change-me" },
    });
    assert.equal(res.statusCode, 200);
    assert.equal(sawTenant, "default");
    assert.equal(res.json().object, "list");
  });

  it("proxies GET /v1/models and echoes X-Request-Id", async () => {
    let sawRequestId: string | null = null;
    globalThis.fetch = (async (input, init) => {
      const headers = init?.headers as Record<string, string>;
      sawRequestId = headers["x-request-id"] ?? null;
      assert.match(String(input), /\/v1\/models$/);
      return new Response(
        JSON.stringify({
          object: "list",
          data: [
            {
              id: "mock-small",
              object: "model",
              created: 1700000000,
              owned_by: "mock",
              purpose: "chat",
            },
          ],
        }),
        {
          status: 200,
          headers: {
            "content-type": "application/json",
            "x-request-id": headers["x-request-id"] ?? "",
          },
        },
      );
    }) as typeof fetch;

    const res = await app.inject({
      method: "GET",
      url: "/v1/models",
      headers: {
        "x-api-key": "demo-key-change-me",
        "x-request-id": "models-gw-001",
      },
    });
    assert.equal(res.statusCode, 200);
    assert.equal(res.headers["x-request-id"], "models-gw-001");
    assert.equal(sawRequestId, "models-gw-001");
    assert.equal(res.json().data[0].id, "mock-small");
  });

  it("proxies POST /v1/embeddings", async () => {
    globalThis.fetch = (async (input, init) => {
      assert.match(String(input), /\/v1\/embeddings$/);
      const parsed = JSON.parse(String(init?.body ?? "{}")) as { model?: string };
      assert.equal(parsed.model, "text-embedding-3-small");
      return new Response(
        JSON.stringify({
          object: "list",
          data: [{ object: "embedding", embedding: [0.1, 0.2], index: 0 }],
          model: "text-embedding-3-small",
          usage: { prompt_tokens: 2, total_tokens: 2 },
          embedding_provider: "hashing",
          dim: 2,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }) as typeof fetch;

    const res = await app.inject({
      method: "POST",
      url: "/v1/embeddings",
      headers: { "x-api-key": "demo-key-change-me" },
      payload: { model: "text-embedding-3-small", input: "hello world" },
    });
    assert.equal(res.statusCode, 200);
    assert.equal(res.json().dim, 2);
    assert.equal(res.json().data[0].embedding.length, 2);
  });

  it("rejects embeddings without input", async () => {
    const res = await app.inject({
      method: "POST",
      url: "/v1/embeddings",
      headers: { "x-api-key": "demo-key-change-me" },
      payload: { model: "text-embedding-3-small" },
    });
    assert.equal(res.statusCode, 400);
  });

  it("proxies SSE streams without buffering JSON", async () => {
    const sse =
      'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"mock-small","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}],"provider":"mock"}\n\n' +
      'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"mock-small","choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}],"provider":"mock"}\n\n' +
      "data: [DONE]\n\n";

    let sawStreamBody = false;
    globalThis.fetch = (async (_input, init) => {
      const parsed = JSON.parse(String(init?.body ?? "{}")) as { stream?: boolean };
      assert.equal(parsed.stream, true);
      sawStreamBody = true;
      return new Response(sse, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    }) as typeof fetch;

    const res = await app.inject({
      method: "POST",
      url: "/v1/chat/completions",
      headers: { "x-api-key": "demo-key-change-me" },
      payload: {
        model: "mock-small",
        messages: [{ role: "user", content: "hi" }],
        stream: true,
      },
    });

    assert.equal(sawStreamBody, true);
    assert.equal(res.statusCode, 200);
    assert.match(res.headers["content-type"] ?? "", /text\/event-stream/);
    assert.match(res.body, /data: \[DONE\]/);
    assert.match(res.body, /"content":"hi"/);
    // Must not have been parsed/re-serialized as a single JSON object.
    assert.equal(res.headers["content-type"]?.includes("application/json"), false);
  });
});

describe("parseTenantApiKeys", () => {
  it("parses compact and JSON maps", () => {
    assert.deepEqual(parseTenantApiKeys("a:alpha,b:beta"), {
      a: "alpha",
      b: "beta",
    });
    assert.deepEqual(parseTenantApiKeys('{"k":"t"}'), { k: "t" });
    assert.deepEqual(parseTenantApiKeys(""), {});
  });
});

describe("extractApiKey", () => {
  it("prefers X-API-Key over Bearer", () => {
    assert.equal(
      extractApiKey({ "x-api-key": "from-header", authorization: "Bearer from-bearer" }),
      "from-header",
    );
  });

  it("reads Authorization Bearer", () => {
    assert.equal(extractApiKey({ authorization: "Bearer sk-demo" }), "sk-demo");
    assert.equal(extractApiKey({ authorization: "bearer sk-demo" }), "sk-demo");
    assert.equal(extractApiKey({}), undefined);
  });
});

describe("gateway rate limiting", () => {
  const originalFetch = globalThis.fetch;

  after(() => {
    globalThis.fetch = originalFetch;
  });

  function mockOkFetch(): void {
    globalThis.fetch = (async () =>
      new Response(
        JSON.stringify({
          id: "chatcmpl-rl",
          object: "chat.completion",
          created: 1,
          model: "mock-small",
          choices: [
            {
              index: 0,
              message: { role: "assistant", content: "ok" },
              finish_reason: "stop",
            },
          ],
          usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
          provider: "mock",
          cached: false,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      )) as typeof fetch;
  }

  it("returns 429 when over the per-key burst", async () => {
    resetMetricsForTests();
    mockOkFetch();
    const limiter = new MemoryRateLimiter();
    const app = await buildServer(
      loadConfig({
        PORT: "0",
        ROUTER_URL: "http://router.test",
        DEMO_API_KEY: "demo-key-change-me",
        LOG_LEVEL: "silent",
        RATE_LIMIT_ENABLED: "true",
        RATE_LIMIT_BACKEND: "memory",
        RATE_LIMIT_KEY_QPS: "1",
        RATE_LIMIT_KEY_BURST: "2",
        RATE_LIMIT_TENANT_QPS: "0",
      }),
      { rateLimiter: limiter },
    );
    await app.ready();
    try {
      const payload = {
        model: "mock-small",
        messages: [{ role: "user", content: "hi" }],
      };
      const headers = { "x-api-key": "demo-key-change-me" };
      const first = await app.inject({
        method: "POST",
        url: "/v1/chat/completions",
        headers,
        payload,
      });
      const second = await app.inject({
        method: "POST",
        url: "/v1/chat/completions",
        headers,
        payload,
      });
      const third = await app.inject({
        method: "POST",
        url: "/v1/chat/completions",
        headers,
        payload,
      });

      assert.equal(first.statusCode, 200);
      assert.equal(second.statusCode, 200);
      assert.equal(third.statusCode, 429);
      assert.equal(third.json().error.type, "rate_limit_error");
      assert.equal(third.json().error.scope, "key");
      assert.equal(third.headers["x-ratelimit-scope"], "key");
      assert.ok(Number(third.headers["retry-after"]) >= 1);

      const metrics = await app.inject({ method: "GET", url: "/metrics" });
      assert.match(metrics.body, /gateway_rate_limit_rejected_total/);
    } finally {
      await app.close();
    }
  });

  it("leaves under-limit traffic unchanged when disabled", async () => {
    mockOkFetch();
    const app = await buildServer(
      loadConfig({
        PORT: "0",
        ROUTER_URL: "http://router.test",
        DEMO_API_KEY: "demo-key-change-me",
        LOG_LEVEL: "silent",
        RATE_LIMIT_ENABLED: "false",
        RATE_LIMIT_KEY_QPS: "1",
        RATE_LIMIT_KEY_BURST: "1",
      }),
    );
    await app.ready();
    try {
      for (let i = 0; i < 5; i += 1) {
        const res = await app.inject({
          method: "POST",
          url: "/v1/chat/completions",
          headers: { "x-api-key": "demo-key-change-me" },
          payload: {
            model: "mock-small",
            messages: [{ role: "user", content: `hi ${i}` }],
          },
        });
        assert.equal(res.statusCode, 200);
        assert.equal(res.headers["x-ratelimit-limit"], undefined);
      }
    } finally {
      await app.close();
    }
  });

  it("enforces per-tenant limits across keys", async () => {
    mockOkFetch();
    const limiter = new MemoryRateLimiter();
    const app = await buildServer(
      loadConfig({
        PORT: "0",
        ROUTER_URL: "http://router.test",
        TENANT_API_KEYS: "key-a:acme,key-b:acme",
        LOG_LEVEL: "silent",
        RATE_LIMIT_ENABLED: "true",
        RATE_LIMIT_BACKEND: "memory",
        RATE_LIMIT_KEY_QPS: "0",
        RATE_LIMIT_TENANT_QPS: "1",
        RATE_LIMIT_TENANT_BURST: "1",
      }),
      { rateLimiter: limiter },
    );
    await app.ready();
    try {
      const payload = {
        model: "mock-small",
        messages: [{ role: "user", content: "hi" }],
      };
      const a = await app.inject({
        method: "POST",
        url: "/v1/chat/completions",
        headers: { "x-api-key": "key-a" },
        payload,
      });
      const b = await app.inject({
        method: "POST",
        url: "/v1/chat/completions",
        headers: { "x-api-key": "key-b" },
        payload,
      });
      assert.equal(a.statusCode, 200);
      assert.equal(b.statusCode, 429);
      assert.equal(b.json().error.scope, "tenant");
    } finally {
      await app.close();
    }
  });
});

describe("MemoryRateLimiter", () => {
  it("refills tokens over time", async () => {
    const limiter = new MemoryRateLimiter();
    const first = await limiter.take("key", "k1", 100, 1);
    assert.equal(first.allowed, true);
    const second = await limiter.take("key", "k1", 100, 1);
    assert.equal(second.allowed, false);
    await new Promise((r) => setTimeout(r, 25));
    const third = await limiter.take("key", "k1", 100, 1);
    assert.equal(third.allowed, true);
    await limiter.close();
  });
});
