"""OTel trace-context propagation helpers (P0.12, RFC-001 §6.5).

These helpers are what carry the trace across the custom outbox→stream hop. Fully offline: no
exporter/collector needed — only the W3C propagator and an in-process SDK span.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from relay.core.observability.tracing import extract_context, inject_trace_context


def test_no_active_span_yields_empty_carrier() -> None:
    # With no recording span in context (tracing disabled), inject writes nothing — so emit() adds
    # no `_trace` key and payloads are left untouched.
    assert inject_trace_context() == {}


def test_inject_then_extract_roundtrips_trace_id() -> None:
    provider = TracerProvider()
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("emit") as span:
        carrier = inject_trace_context()
        expected_trace_id = span.get_span_context().trace_id

    assert "traceparent" in carrier

    ctx = extract_context(carrier)
    propagated = trace.get_current_span(ctx).get_span_context()
    assert propagated.trace_id == expected_trace_id
