from __future__ import annotations

import logging
from typing import Any

from opentelemetry import context, propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.trace import Span, Status, StatusCode, Tracer

from app.config import Settings
from app.config import settings as default_settings

logger = logging.getLogger(__name__)

_initialized = False


def init_tracing(settings: Settings | None = None) -> None:
    """Configure tracer provider once. No OTLP exporter unless endpoint is set."""
    global _initialized
    if _initialized:
        return

    cfg = settings or default_settings
    if not cfg.otel_enabled:
        _initialized = True
        return

    resource = Resource.create({"service.name": cfg.otel_service_name})
    provider = TracerProvider(resource=resource)

    endpoint = (cfg.otel_exporter_otlp_endpoint or "").strip().rstrip("/")
    if endpoint:
        # OTLP HTTP exporter expects the full traces URL.
        exporter = OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")
        provider.add_span_processor(BatchSpanProcessor(exporter))
        logger.info("otel otlp exporter endpoint=%s", endpoint)
    if cfg.otel_console_exporter:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    _initialized = True


def get_tracer(name: str = "router") -> Tracer:
    return trace.get_tracer(name)


def extract_context(headers: Any) -> context.Context:
    """Extract W3C trace context from inbound HTTP headers."""
    carrier: dict[str, str] = {}
    try:
        for key, value in headers.items():
            if isinstance(value, str):
                carrier[str(key).lower()] = value
            elif isinstance(value, (list, tuple)) and value:
                carrier[str(key).lower()] = str(value[0])
    except Exception:  # noqa: BLE001
        return context.get_current()
    return propagate.extract(carrier)


def attach_context(ctx: context.Context) -> Any:
    return context.attach(ctx)


def detach_context(token: Any) -> None:
    try:
        context.detach(token)
    except Exception:  # noqa: BLE001
        pass


def set_span_error(span: Span, err: BaseException | str) -> None:
    if isinstance(err, BaseException):
        span.record_exception(err)
        span.set_status(Status(StatusCode.ERROR, str(err)))
    else:
        span.set_status(Status(StatusCode.ERROR, err))


def set_span_ok(span: Span) -> None:
    span.set_status(Status(StatusCode.OK))
