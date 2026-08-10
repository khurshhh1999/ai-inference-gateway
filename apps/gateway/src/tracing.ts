import { context, propagation, SpanStatusCode, trace, type Span, type Tracer } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { W3CTraceContextPropagator } from "@opentelemetry/core";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { Resource } from "@opentelemetry/resources";
import {
  BasicTracerProvider,
  BatchSpanProcessor,
  ConsoleSpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { ATTR_SERVICE_NAME } from "@opentelemetry/semantic-conventions";

export type TracingConfig = {
  enabled: boolean;
  serviceName: string;
  /** OTLP HTTP base, e.g. http://jaeger:4318 — exporter posts to /v1/traces */
  otlpEndpoint: string;
  /** Also print spans to stdout (local debug). */
  consoleExporter: boolean;
};

let provider: BasicTracerProvider | null = null;
let contextManagerReady = false;

function ensureContextManager(): void {
  if (contextManagerReady) return;
  context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
  contextManagerReady = true;
}

export function loadTracingConfig(env: NodeJS.ProcessEnv = process.env): TracingConfig {
  const enabledRaw = (env.OTEL_ENABLED ?? "true").trim().toLowerCase();
  const enabled = !["0", "false", "no", "off"].includes(enabledRaw);
  const consoleRaw = (env.OTEL_CONSOLE_EXPORTER ?? "false").trim().toLowerCase();
  return {
    enabled,
    serviceName: (env.OTEL_SERVICE_NAME ?? "gateway").trim() || "gateway",
    otlpEndpoint: (env.OTEL_EXPORTER_OTLP_ENDPOINT ?? "").trim().replace(/\/$/, ""),
    consoleExporter: ["1", "true", "yes", "on"].includes(consoleRaw),
  };
}

/** Initialize the tracer provider once (safe to call repeatedly). */
export function initTracing(config: TracingConfig = loadTracingConfig()): void {
  ensureContextManager();
  propagation.setGlobalPropagator(new W3CTraceContextPropagator());
  if (!config.enabled) {
    return;
  }
  if (provider) {
    return;
  }

  const resource = new Resource({
    [ATTR_SERVICE_NAME]: config.serviceName,
  });
  const tracerProvider = new BasicTracerProvider({ resource });

  if (config.otlpEndpoint) {
    const url = `${config.otlpEndpoint}/v1/traces`;
    tracerProvider.addSpanProcessor(
      new BatchSpanProcessor(new OTLPTraceExporter({ url })),
    );
  }
  if (config.consoleExporter) {
    tracerProvider.addSpanProcessor(new SimpleSpanProcessor(new ConsoleSpanExporter()));
  }

  trace.setGlobalTracerProvider(tracerProvider);
  provider = tracerProvider;
}

export async function shutdownTracing(): Promise<void> {
  if (!provider) return;
  await provider.shutdown();
  provider = null;
}

export function getTracer(name = "gateway"): Tracer {
  return trace.getTracer(name);
}

/** Inject W3C trace headers. Pass `span` when not running under an active context. */
export function injectTraceHeaders(headers: Record<string, string>, span?: Span): void {
  const ctx = span ? trace.setSpan(context.active(), span) : context.active();
  propagation.inject(ctx, headers, {
    set: (carrier, key, value) => {
      carrier[key] = value;
    },
  });
}

export function startRequestSpan(
  tracer: Tracer,
  name: string,
  attributes: Record<string, string | number | boolean>,
): Span {
  return tracer.startSpan(name, { attributes });
}

export function endSpanOk(span: Span): void {
  span.setStatus({ code: SpanStatusCode.OK });
  span.end();
}

export function endSpanError(span: Span, err: unknown): void {
  if (err instanceof Error) {
    span.recordException(err);
  }
  span.setStatus({
    code: SpanStatusCode.ERROR,
    message: err instanceof Error ? err.message : String(err),
  });
  span.end();
}

export function withSpanContext<T>(span: Span, fn: () => T): T {
  return context.with(trace.setSpan(context.active(), span), fn);
}
