import { Readable } from "node:stream";
import Fastify, {
  type FastifyInstance,
  type FastifyReply,
  type FastifyRequest,
} from "fastify";
import { resolveTenant } from "./auth.js";
import type { GatewayConfig } from "./config.js";
import {
  authFailures,
  bodyRejected,
  metricsPayload,
  requestDuration,
  upstreamErrors,
} from "./metrics.js";
import { openApiDocument, swaggerUiHtml } from "./openapi.js";

type ChatBody = {
  model: string;
  messages: Array<{ role: string; content: string }>;
  stream?: boolean;
  max_tokens?: number;
  temperature?: number;
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
  if (PUBLIC_PATHS.has(path)) return path;
  return "other";
}

export async function buildServer(config: GatewayConfig): Promise<FastifyInstance> {
  const app = Fastify({
    logger: { level: config.logLevel },
    bodyLimit: config.maxBodyBytes,
    requestTimeout: 120_000,
  });

  app.setErrorHandler((err, request, reply) => {
    const statusCode =
      typeof err === "object" && err && "statusCode" in err
        ? Number((err as { statusCode?: number }).statusCode)
        : 500;
    if (statusCode === 413) {
      bodyRejected.inc({ reason: "too_large" });
      return reply.code(413).send({
        error: {
          message: `Request body exceeds limit of ${config.maxBodyBytes} bytes`,
          type: "invalid_request_error",
        },
      });
    }
    request.log.error({ err }, "request error");
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
    (request as FastifyRequest & { _startedAt?: number })._startedAt = performance.now();
    const path = (request.url.split("?")[0] ?? request.url) as string;
    if (PUBLIC_PATHS.has(path)) {
      return;
    }
    const key = request.headers["x-api-key"];
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
    (request as FastifyRequest & { tenantId?: string }).tenantId = tenant;
  });

  app.addHook("onResponse", async (request, reply) => {
    const started = (request as FastifyRequest & { _startedAt?: number })._startedAt;
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

    const tenantId =
      (request as FastifyRequest & { tenantId?: string }).tenantId ?? "default";

    const forwardHeaders: Record<string, string> = {
      "content-type": "application/json",
      "x-tenant-id": tenantId,
    };
    const cacheBypass = request.headers["x-cache-bypass"];
    if (typeof cacheBypass === "string" && cacheBypass.length > 0) {
      forwardHeaders["x-cache-bypass"] = cacheBypass;
    }

    const wantStream = Boolean(body.stream);
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
        return reply;
      }
      upstreamErrors.inc({ kind: "unreachable" });
      request.log.error({ err }, "router unreachable");
      return reply.code(502).send({
        error: {
          message: "Router unreachable",
          type: "upstream_error",
        },
      });
    }

    if (upstream.status >= 500) {
      upstreamErrors.inc({ kind: "upstream_5xx" });
    }

    // Surface soft budget warnings from the router.
    const budgetWarning = upstream.headers.get("x-budget-warning");
    if (budgetWarning) {
      reply.header("x-budget-warning", budgetWarning);
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
        request.log.error({ err }, "upstream SSE stream error");
        abort.abort();
      });
      nodeStream.on("close", () => {
        request.raw.off("aborted", onClientGone);
        reply.raw.off("close", onClientGone);
      });
      nodeStream.on("end", () => {
        request.raw.off("aborted", onClientGone);
        reply.raw.off("close", onClientGone);
      });

      return reply.send(nodeStream);
    }

    try {
      const text = await upstream.text();
      reply.code(upstream.status);
      reply.header("content-type", contentType);
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

  return app;
}
