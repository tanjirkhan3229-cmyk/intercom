"""OpenTelemetry tracing (RFC-001 §6.5/§9): request → outbox → worker correlation.

A **no-op unless an OTLP endpoint is configured** (``settings.otel_enabled``), so dev/tests need
no collector and existing behaviour is unchanged. When enabled we instrument FastAPI, SQLAlchemy,
httpx, Redis, and Celery (the last handles request→task propagation on its own message headers).

The one hop OTel can't see is the custom **outbox → Redis stream → fan-out** path, so we propagate
the W3C trace context by hand: ``emit()`` calls :func:`inject_trace_context` (a no-op with no active
span, so it never touches payloads in tests), the relay strips the carrier back out before it
publishes (so it never leaks to clients) and uses it to parent the publish span.
"""

from __future__ import annotations

from typing import Any

from relay.core.logging import get_logger
from relay.settings import get_settings

log = get_logger(__name__)

# Reserved payload key carrying the injected W3C trace context between emit() and the relay.
TRACE_CARRIER_KEY = "_trace"

_configured = False


def configure_tracing() -> bool:
    """Set up the tracer provider + OTLP exporter and instrument libraries. Idempotent; returns
    whether tracing is active."""
    global _configured
    settings = get_settings()
    if not settings.otel_enabled or _configured:
        return _configured

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "deployment.environment": settings.environment,
            "service.version": settings.deploy_sha,
        }
    )
    provider = TracerProvider(
        resource=resource, sampler=TraceIdRatioBased(settings.otel_traces_sampler_ratio)
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint))
    )
    trace.set_tracer_provider(provider)
    _instrument_libraries()
    _configured = True
    log.info("tracing.configured", endpoint=settings.otel_exporter_otlp_endpoint)
    return True


def _instrument_libraries() -> None:
    from opentelemetry.instrumentation.celery import CeleryInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    from relay.core.db import get_engine

    # The async engine wraps a sync Engine that the instrumentor hooks.
    SQLAlchemyInstrumentor().instrument(engine=get_engine().sync_engine)
    HTTPXClientInstrumentor().instrument()
    RedisInstrumentor().instrument()
    CeleryInstrumentor().instrument()  # type: ignore[no-untyped-call]


def instrument_fastapi(app: Any) -> None:
    """Instrument a FastAPI app instance (server spans). No-op when tracing is disabled."""
    if not get_settings().otel_enabled:
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app)


def inject_trace_context() -> dict[str, str]:
    """Return a carrier holding the current W3C trace context. Empty when there is no active
    (recording) span — which is the case whenever tracing is disabled, so callers can add it to a
    payload only when non-empty and leave payloads untouched otherwise."""
    from opentelemetry.propagate import inject

    carrier: dict[str, str] = {}
    inject(carrier)
    return carrier


def extract_context(carrier: dict[str, str]) -> Any:
    """Rebuild an OTel context from a carrier made by :func:`inject_trace_context`."""
    from opentelemetry.propagate import extract

    return extract(carrier)
