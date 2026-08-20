import { Readable } from "node:stream";
import Fastify, {
  type FastifyInstance,
  type FastifyReply,
  type FastifyRequest,
} from "fastify";
import { resolveTenant, extractApiKey } from "./auth.js";
import type { GatewayConfig } from "./config.js";
import {
  authFailures,
  bodyRejected,
  metricsPayload,
  rateLimitRedisErrors,
  rateLimitRejected,
  requestDuration,
  upstreamErrors,
} from "./metrics.js";
import { openApiDocument, swaggerUiHtml } from "./openapi.js";
import {
  createRateLimiter,
  enforceRateLimits,
  hashApiKey,
  type RateLimiter,
} from "./rateLimit.js";
import { proxyJsonToRouter } from "./proxy.js";
import { resolveRequestId } from "./requestId.js";
import {
  endSpanError,
  endSpanOk,
  getTracer,
  initTracing,
  injectTraceHeaders,
  startRequestSpan,
  withSpanContext,
} from "./tracing.js";

type ChatMessage = {
  role: string;
  content?: string | null;
  name?: string;
  tool_call_id?: string;
  tool_calls?: unknown[];
};

type ChatBody = {
  model: string;
  messages: ChatMessage[];
  stream?: boolean;
  max_tokens?: number;
  temperature?: number;
  tools?: unknown[];
  tool_choice?: unknown;
};

type EmbeddingsBody = {
  model: string;
  input: string | string[];
  encoding_format?: string;
  dimensions?: number;
  user?: string;
};

type RequestExtras = FastifyRequest & {
  _startedAt?: number;
  tenantId?: string;
  apiKeyHash?: string;
  requestId?: string;
};

const PUBLIC_PATHS = new Set([
  "/health",
  "/metrics",
  "/openapi.json",
  "/docs",
]);

function routeLabel(url: string): string {
  const path = url.split("?")[0] ?? url;
  if (path === "/v1/chat/completions") return "/v1/chat/completions";
  if (path === "/v1/embeddings") return "/v1/embeddings";
  if (path === "/v1/models" || path.startsWith("/v1/models/")) return "/v1/models";
  if (PUBLIC_PATHS.has(path)) return path;
  return "other";
}

export type BuildServerOptions = {
  /** Override rate limiter (tests). */
  rateLimiter?: RateLimiter;
};

export async function buildServer(
  config: GatewayConfig,
  options: BuildServerOptions = {},
): Promise<FastifyInstance> {
  initTracing({
    enabled: config.otelEnabled,
    serviceName: config.otelServiceName,
    otlpEndpoint: config.otelExporterOtlpEndpoint,
    consoleExporter: config.otelConsoleExporter,
  });

  const app = Fastify({
    logger: { level: config.logLevel },
    bodyLimit: config.maxBodyBytes,
    requestTimeout: 120_000,
  });
  const tracer = getTracer("gateway");

  const rateLimiter =
    options.rateLimiter ??
    createRateLimiter(
      {
        enabled: config.rateLimitEnabled,
        backend: config.rateLimitBackend,
        redisUrl: config.redisUrl,
        keyQps: config.rateLimitKeyQps,
        keyBurst: config.rateLimitKeyBurst,
        tenantQps: config.rateLimitTenantQps,
        tenantBurst: config.rateLimitTenantBurst,
      },
      {
        onRedisError: () => {
          rateLimitRedisErrors.inc();
        },
      },
    );

  app.addHook("onClose", async () => {
    await rateLimiter.close();
  });

  app.setErrorHandler((err, request, reply) => {
    const statusCode =
      typeof err === "object" && err && "statusCode" in err
        ? Number((err as { statusCode?: number }).statusCode)
        : 500;
    const requestId = (request as RequestExtras).requestId;
    if (requestId) {
      void reply.header("x-request-id", requestId);
    }
    if (statusCode === 413) {
      bodyRejected.inc({ reason: "too_large" });
      return reply.code(413).send({
        error: {
          message: `Request body exceeds limit of ${config.maxBodyBytes} bytes`,
          type: "invalid_request_error",
        },
      });
    }
    request.log.error({ err, request_id: requestId }, "request error");
    if (!reply.sent) {
      return reply.code(statusCode >= 400 ? statusCode : 500).send({
        error: {
          message: err instanceof Error ? err.message : "Internal error",
          type: "server_error",
        },
      });
    }
  });

  app.addHook("onRequest", async (request: FastifyRequest, reply: FastifyReply) => {
    const extras = request as RequestExtras;
    extras._startedAt = performance.now();
    const requestId = resolveRequestId(request.headers["x-request-id"]);
    extras.requestId = requestId;
    void reply.header("x-request-id", requestId);

    const path = (request.url.split("?")[0] ?? request.url) as string;
    if (PUBLIC_PATHS.has(path)) {
      return;
    }
    const key = extractApiKey(request.headers as Record<string, unknown>);
    if (typeof key !== "string") {
      authFailures.inc();
      return reply.code(401).send({
        error: {
          message: "Missing or invalid X-API-Key",
          type: "authentication_error",
        },
      });
    }
    const tenant = resolveTenant(key, config.tenantApiKeys);
    if (tenant === null) {
      authFailures.inc();
      return reply.code(401).send({
        error: {
          message: "Missing or invalid X-API-Key",
          type: "authentication_error",
        },
      });
    }
    extras.tenantId = tenant;
    extras.apiKeyHash = hashApiKey(key);

    const decision = await enforceRateLimits(
      rateLimiter,
      {
        enabled: config.rateLimitEnabled,
        backend: config.rateLimitBackend,
        redisUrl: config.redisUrl,
        keyQps: config.rateLimitKeyQps,
        keyBurst: config.rateLimitKeyBurst,
        tenantQps: config.rateLimitTenantQps,
        tenantBurst: config.rateLimitTenantBurst,
      },
      extras.apiKeyHash,
      tenant,
    );

    if (decision) {
      void reply.header("x-ratelimit-limit", String(decision.limit));
      void reply.header("x-ratelimit-remaining", String(Math.max(0, decision.remaining)));
      void reply.header("x-ratelimit-scope", decision.scope);
      if (!decision.allowed) {
        rateLimitRejected.inc({ scope: decision.scope });
        const retryAfterSec = Math.max(1, Math.ceil(decision.retryAfterMs / 1000));
        void reply.header("retry-after", String(retryAfterSec));
        return reply.code(429).send({
          error: {
            message: `Rate limit exceeded (${decision.scope})`,
            type: "rate_limit_error",
            scope: decision.scope,
            limit: decision.limit,
            retry_after_ms: decision.retryAfterMs,
          },
        });
      }
    }
  });

  app.addHook("onResponse", async (request, reply) => {
    const started = (request as RequestExtras)._startedAt;
    if (started === undefined) return;
    const seconds = (performance.now() - started) / 1000;
    requestDuration.observe(
      {
        method: request.method,
        route: routeLabel(request.url),
        status: String(reply.statusCode),
      },
      seconds,
    );
  });

  app.get("/health", async () => ({
    status: "ok",
    service: "gateway",
    routerUrl: config.routerUrl,
  }));

  app.get("/metrics", async (_request, reply) => {
    const { body, contentType } = await metricsPayload();
    return reply.type(contentType).send(body);
  });

  app.get("/openapi.json", async () => openApiDocument);

  app.get("/docs", async (_request, reply) =>
    reply.type("text/html; charset=utf-8").send(swaggerUiHtml),
  );

  app.post<{ Body: ChatBody }>("/v1/chat/completions", async (request, reply) => {
    const extras = request as RequestExtras;
    const requestId = extras.requestId ?? resolveRequestId(undefined);
    const body = request.body;
    if (!body?.model || !Array.isArray(body.messages) || body.messages.length === 0) {
      bodyRejected.inc({ reason: "invalid_schema" });
      return reply.code(400).send({
        error: {
          message: "Request must include model and a non-empty messages array",
          type: "invalid_request_error",
        },
      });
    }

    const tenantId = extras.tenantId ?? "default";
    const wantStream = Boolean(body.stream);

    const span = startRequestSpan(tracer, "gateway.chat.completions", {
      "http.request_id": requestId,
      "tenant.id": tenantId,
      "llm.model": body.model,
      "llm.stream": wantStream,
      "http.route": "/v1/chat/completions",
    });

    return withSpanContext(span, async () => {
      const forwardHeaders: Record<string, string> = {
        "content-type": "application/json",
        "x-tenant-id": tenantId,
        "x-request-id": requestId,
      };
      const cacheBypass = request.headers["x-cache-bypass"];
      if (typeof cacheBypass === "string" && cacheBypass.length > 0) {
        forwardHeaders["x-cache-bypass"] = cacheBypass;
      }
      injectTraceHeaders(forwardHeaders, span);

      const abort = new AbortController();
      // Abort upstream only on client disconnect — not on request 'close', which
      // fires after the body is fully read and would cancel every proxy call.
      const onClientGone = () => {
        if (!reply.raw.writableEnded) {
          abort.abort();
        }
      };
      request.raw.on("aborted", onClientGone);
      reply.raw.on("close", onClientGone);

      let upstream: Response;
      try {
        upstream = await fetch(`${config.routerUrl}/v1/chat/completions`, {
          method: "POST",
          headers: forwardHeaders,
          body: JSON.stringify(body),
          signal: abort.signal,
        });
      } catch (err) {
        request.raw.off("aborted", onClientGone);
        reply.raw.off("close", onClientGone);
        if (abort.signal.aborted) {
          endSpanOk(span);
          return reply;
        }
        upstreamErrors.inc({ kind: "unreachable" });
        request.log.error({ err, request_id: requestId }, "router unreachable");
        endSpanError(span, err);
        return reply.code(502).send({
          error: {
            message: "Router unreachable",
            type: "upstream_error",
          },
        });
      }

      span.setAttribute("http.status_code", upstream.status);
      if (upstream.status >= 500) {
        upstreamErrors.inc({ kind: "upstream_5xx" });
      }

      // Surface soft budget warnings from the router.
      const budgetWarning = upstream.headers.get("x-budget-warning");
      if (budgetWarning) {
        reply.header("x-budget-warning", budgetWarning);
      }
      const upstreamRequestId = upstream.headers.get("x-request-id");
      if (upstreamRequestId) {
        reply.header("x-request-id", upstreamRequestId);
      } else {
        reply.header("x-request-id", requestId);
      }

      const contentType = upstream.headers.get("content-type") ?? "application/json";
      const isEventStream =
        wantStream && contentType.toLowerCase().includes("text/event-stream");

      if (isEventStream && upstream.body) {
        // Pipe SSE without buffering; headers flush as soon as the stream starts.
        reply.code(upstream.status);
        reply.header("content-type", "text/event-stream");
        reply.header("cache-control", "no-cache");
        reply.header("connection", "keep-alive");
        reply.header("x-accel-buffering", "no");

        const nodeStream = Readable.fromWeb(
          upstream.body as import("node:stream/web").ReadableStream,
        );

        nodeStream.on("error", (err) => {
          request.log.error({ err, request_id: requestId }, "upstream SSE stream error");
          abort.abort();
          endSpanError(span, err);
        });
        nodeStream.on("close", () => {
          request.raw.off("aborted", onClientGone);
          reply.raw.off("close", onClientGone);
        });
        nodeStream.on("end", () => {
          request.raw.off("aborted", onClientGone);
          reply.raw.off("close", onClientGone);
          if (upstream.status >= 500) {
            endSpanError(span, new Error(`upstream status ${upstream.status}`));
          } else {
            endSpanOk(span);
          }
        });

        return reply.send(nodeStream);
      }

      try {
        const text = await upstream.text();
        reply.code(upstream.status);
        reply.header("content-type", contentType);
        if (upstream.status >= 500) {
          endSpanError(span, new Error(`upstream status ${upstream.status}`));
        } else {
          endSpanOk(span);
        }
        if (!text.length) {
          return reply.send(null);
        }
        try {
          return reply.send(JSON.parse(text));
        } catch {
          return reply.send({
            error: { message: text, type: "upstream_error" },
          });
        }
      } finally {
        request.raw.off("aborted", onClientGone);
        reply.raw.off("close", onClientGone);
      }
    });
  });

  app.get("/v1/models", async (request, reply) => {
    const extras = request as RequestExtras;
    const requestId = extras.requestId ?? resolveRequestId(undefined);
    const tenantId = extras.tenantId ?? "default";
    return proxyJsonToRouter(request, reply, config, tracer, requestId, tenantId, {
      method: "GET",
      upstreamPath: "/v1/models",
      spanName: "gateway.models.list",
      route: "/v1/models",
    });
  });

  app.get("/v1/models/:modelId", async (request, reply) => {
    const extras = request as RequestExtras;
    const requestId = extras.requestId ?? resolveRequestId(undefined);
    const tenantId = extras.tenantId ?? "default";
    const { modelId } = request.params as { modelId: string };
    return proxyJsonToRouter(request, reply, config, tracer, requestId, tenantId, {
      method: "GET",
      upstreamPath: `/v1/models/${encodeURIComponent(modelId)}`,
      spanName: "gateway.models.retrieve",
      route: "/v1/models",
      spanAttrs: { "llm.model": modelId },
    });
  });

  app.post<{ Body: EmbeddingsBody }>("/v1/embeddings", async (request, reply) => {
    const extras = request as RequestExtras;
    const requestId = extras.requestId ?? resolveRequestId(undefined);
    const body = request.body;
    if (!body?.model || body.input === undefined || body.input === null) {
      bodyRejected.inc({ reason: "invalid_schema" });
      return reply.code(400).send({
        error: {
          message: "Request must include model and input",
          type: "invalid_request_error",
        },
      });
    }
    const tenantId = extras.tenantId ?? "default";
    return proxyJsonToRouter(request, reply, config, tracer, requestId, tenantId, {
      method: "POST",
      upstreamPath: "/v1/embeddings",
      spanName: "gateway.embeddings",
      route: "/v1/embeddings",
      body,
      spanAttrs: { "llm.model": body.model },
    });
  });

  return app;
}
