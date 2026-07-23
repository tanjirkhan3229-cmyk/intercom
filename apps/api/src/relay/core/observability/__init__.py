"""Observability kernel (P0.12, RFC-001 §9/§13): metrics, tracing, error tracking, PII scrubbing.

All of it degrades to a no-op when unconfigured (no OTLP endpoint / no Sentry DSN), so dev and
tests run without any collector. ``init_app_observability(app)`` wires the `app` shape;
non-HTTP shapes call the individual ``configure_*`` / ``start_metrics_server`` helpers.
"""

from __future__ import annotations

from typing import Any

from relay.core.observability.metrics import (
    MetricsMiddleware,
    register_celery_metrics,
    start_metrics_server,
)
from relay.core.observability.scrub import scrub, scrub_processor, sentry_before_send
from relay.core.observability.sentry import configure_sentry
from relay.core.observability.tracing import (
    TRACE_CARRIER_KEY,
    configure_tracing,
    extract_context,
    inject_trace_context,
    instrument_fastapi,
)

__all__ = [
    "TRACE_CARRIER_KEY",
    "MetricsMiddleware",
    "configure_sentry",
    "configure_tracing",
    "extract_context",
    "init_app_observability",
    "init_worker_observability",
    "inject_trace_context",
    "instrument_fastapi",
    "register_celery_metrics",
    "scrub",
    "scrub_processor",
    "sentry_before_send",
    "start_metrics_server",
]


def init_app_observability(app: Any) -> None:
    """Wire the `app` runtime shape: Sentry, tracing, FastAPI instrumentation. The /metrics route
    and ``MetricsMiddleware`` are mounted by ``relay.main`` directly."""
    configure_sentry()
    configure_tracing()
    instrument_fastapi(app)


def init_worker_observability() -> None:
    """Wire a Celery `workers`/`beat` shape: logging is configured by the caller; here we add
    Sentry, tracing (Celery instrumentation), and task metrics signals."""
    configure_sentry()
    configure_tracing()
    register_celery_metrics()
