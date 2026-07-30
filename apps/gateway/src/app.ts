import { Readable } from "node:stream";
import Fastify, {
  type FastifyInstance,
  type FastifyReply,
  type FastifyRequest,
} from "fastify";
import type { GatewayConfig } from "./config.js";

type ChatBody = {
  model: string;
  messages: Array<{ role: string; content: string }>;
  stream?: boolean;
  max_tokens?: number;
  temperature?: number;
};

export async function buildServer(config: GatewayConfig): Promise<FastifyInstance> {
  const app = Fastify({ logger: { level: config.logLevel } });

  app.addHook("onRequest", async (request: FastifyRequest, reply: FastifyReply) => {
    if (request.url.startsWith("/health")) {
      return;
    }
    const key = request.headers["x-api-key"];
    if (typeof key !== "string" || key !== config.demoApiKey) {
      return reply.code(401).send({
        error: {
          message: "Missing or invalid X-API-Key",
          type: "authentication_error",
        },
      });
    }
  });

  app.get("/health", async () => ({
    status: "ok",
    service: "gateway",
    routerUrl: config.routerUrl,
  }));

  app.post<{ Body: ChatBody }>("/v1/chat/completions", async (request, reply) => {
    const body = request.body;
    if (!body?.model || !Array.isArray(body.messages) || body.messages.length === 0) {
      return reply.code(400).send({
        error: {
          message: "Request must include model and a non-empty messages array",
          type: "invalid_request_error",
        },
      });
    }

    const forwardHeaders: Record<string, string> = {
      "content-type": "application/json",
    };
    const tenantId = request.headers["x-tenant-id"];
    if (typeof tenantId === "string" && tenantId.length > 0) {
      forwardHeaders["x-tenant-id"] = tenantId;
    }
    const cacheBypass = request.headers["x-cache-bypass"];
    if (typeof cacheBypass === "string" && cacheBypass.length > 0) {
      forwardHeaders["x-cache-bypass"] = cacheBypass;
    }

    const wantStream = Boolean(body.stream);
    const abort = new AbortController();
    const onClientClose = () => {
      // Cancel upstream fetch when the client disconnects mid-stream.
      if (!reply.raw.writableEnded) {
        abort.abort();
      }
    };
    request.raw.on("close", onClientClose);

    let upstream: Response;
    try {
      upstream = await fetch(`${config.routerUrl}/v1/chat/completions`, {
        method: "POST",
        headers: forwardHeaders,
        body: JSON.stringify(body),
        signal: abort.signal,
      });
    } catch (err) {
      request.raw.off("close", onClientClose);
      if (abort.signal.aborted) {
        return reply;
      }
      request.log.error({ err }, "router unreachable");
      return reply.code(502).send({
        error: {
          message: "Router unreachable",
          type: "upstream_error",
        },
      });
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
        request.raw.off("close", onClientClose);
      });
      nodeStream.on("end", () => {
        request.raw.off("close", onClientClose);
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
      request.raw.off("close", onClientClose);
    }
  });

  return app;
}
