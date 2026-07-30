import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";
import type { FastifyInstance } from "fastify";
import { buildServer } from "../src/app.js";
import { loadConfig } from "../src/config.js";

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

  it("rejects missing API key", async () => {
    const res = await app.inject({
      method: "POST",
      url: "/v1/chat/completions",
      payload: { model: "mock-small", messages: [{ role: "user", content: "hi" }] },
    });
    assert.equal(res.statusCode, 401);
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
