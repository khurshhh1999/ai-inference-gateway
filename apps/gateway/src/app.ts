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

    let upstream: Response;
    try {
      upstream = await fetch(`${config.routerUrl}/v1/chat/completions`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
    } catch (err) {
      request.log.error({ err }, "router unreachable");
      return reply.code(502).send({
        error: {
          message: "Router unreachable",
          type: "upstream_error",
        },
      });
    }

    const text = await upstream.text();
    reply.code(upstream.status);
    const contentType = upstream.headers.get("content-type") ?? "application/json";
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
  });

  return app;
}
