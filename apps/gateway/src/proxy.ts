import type { FastifyReply, FastifyRequest } from "fastify";
import type { Tracer } from "@opentelemetry/api";
import type { GatewayConfig } from "./config.js";
import { upstreamErrors } from "./metrics.js";
import {
  endSpanError,
  endSpanOk,
  injectTraceHeaders,
  startRequestSpan,
  withSpanContext,
} from "./tracing.js";

type JsonProxyOptions = {
  method: "GET" | "POST";
  upstreamPath: string;
  spanName: string;
  route: string;
  body?: unknown;
  extraHeaders?: Record<string, string>;
  spanAttrs?: Record<string, string | number | boolean>;
};

/**
 * Proxy a JSON request to the router (models, embeddings). Chat/SSE stays
 * in the dedicated completions handler so streams are never buffered.
 */
export async function proxyJsonToRouter(
  request: FastifyRequest,
  reply: FastifyReply,
  config: GatewayConfig,
  tracer: Tracer,
  requestId: string,
  tenantId: string,
  opts: JsonProxyOptions,
): Promise<unknown> {
  const span = startRequestSpan(tracer, opts.spanName, {
    "http.request_id": requestId,
    "tenant.id": tenantId,
    "http.route": opts.route,
    ...opts.spanAttrs,
  });

  return withSpanContext(span, async () => {
    const forwardHeaders: Record<string, string> = {
      "x-tenant-id": tenantId,
      "x-request-id": requestId,
      ...opts.extraHeaders,
    };
    if (opts.body !== undefined) {
      forwardHeaders["content-type"] = "application/json";
    }
    injectTraceHeaders(forwardHeaders, span);

    const abort = new AbortController();
    const onClientGone = () => {
      if (!reply.raw.writableEnded) {
        abort.abort();
      }
    };
    request.raw.on("aborted", onClientGone);
    reply.raw.on("close", onClientGone);

    let upstream: Response;
    try {
      upstream = await fetch(`${config.routerUrl}${opts.upstreamPath}`, {
        method: opts.method,
        headers: forwardHeaders,
        body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
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

    const budgetWarning = upstream.headers.get("x-budget-warning");
    if (budgetWarning) {
      reply.header("x-budget-warning", budgetWarning);
    }
    reply.header(
      "x-request-id",
      upstream.headers.get("x-request-id") ?? requestId,
    );

    try {
      const text = await upstream.text();
      const contentType = upstream.headers.get("content-type") ?? "application/json";
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
        return reply.send(JSON.parse(text) as unknown);
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
}
