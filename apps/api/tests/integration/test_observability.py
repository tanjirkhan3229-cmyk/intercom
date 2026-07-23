"""Observability integration tests (P0.12, RFC-001 §9/§6.5).

Runs against the real migrated schema + Redis (testcontainers). Proves: /readyz reports both
dependencies, /metrics serves golden signals, trace context travels in the outbox payload and is
stripped before publish, and the backlog gauges are measurable.
"""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import psycopg
import pytest
from opentelemetry.sdk.trace import TracerProvider

from relay.core import outbox_relay
from relay.core.db import session_scope
from relay.core.observability.tracing import TRACE_CARRIER_KEY
from relay.core.outbox import OUTBOX_STREAM, emit
from relay.core.redis import get_redis_sync
from relay.settings import get_settings

pytestmark = pytest.mark.integration


async def test_readyz_reports_database_and_redis(client: httpx.AsyncClient) -> None:
    resp = await client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["checks"]["database"] is True
    assert body["checks"]["redis"] is True
    assert body["status"] == "ok"


async def test_metrics_endpoint_exposes_golden_signals(client: httpx.AsyncClient) -> None:
    await client.get("/v0/hello")
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    text = resp.text
    for name in (
        "relay_http_requests_total",
        "relay_http_request_duration_seconds",
        "relay_build_info",
    ):
        assert name in text


async def test_trace_context_travels_in_payload_and_is_stripped_on_publish() -> None:
    provider = TracerProvider()
    tracer = provider.get_tracer("test")
    agg_id = uuid4()

    with tracer.start_as_current_span("emit"):
        async with session_scope() as session:
            await emit(
                session,
                aggregate="conversation",
                aggregate_id=agg_id,
                topic="conversation.created",
                payload={"workspace_id": str(uuid4()), "hello": "world"},
            )

    dsn = get_settings().database_url_psycopg

    # The stored outbox row carries the trace context.
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT payload FROM outbox WHERE aggregate_id = %s", (str(agg_id),)
        ).fetchone()
    assert row is not None
    payload = row[0]
    assert TRACE_CARRIER_KEY in payload
    assert "traceparent" in payload[TRACE_CARRIER_KEY]

    # Publishing strips the carrier so it never reaches the stream / downstream clients.
    redis = get_redis_sync()
    with psycopg.connect(dsn) as conn:
        conn.autocommit = False
        pending = [
            r for r in outbox_relay._fetch_pending(conn, 1000) if r["aggregate_id"] == agg_id
        ]
        outbox_relay._publish_to_stream(redis, pending)
        conn.commit()

    entries = redis.xrange(OUTBOX_STREAM)
    ours = [f for _id, f in entries if f["aggregate_id"] == str(agg_id)]
    assert ours, "our row was not published to the stream"
    stream_payload = json.loads(ours[0]["payload"])
    assert TRACE_CARRIER_KEY not in stream_payload
    assert stream_payload["hello"] == "world"

    # Cleanup so the shared session DB stays tidy for other tests.
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DELETE FROM outbox WHERE aggregate_id = %s", (str(agg_id),))


async def test_outbox_backlog_is_measurable() -> None:
    agg_id = uuid4()
    async with session_scope() as session:
        for _ in range(3):
            await emit(
                session,
                aggregate="conversation",
                aggregate_id=agg_id,
                topic="conversation.part.created",
                payload={},
            )

    dsn = get_settings().database_url_psycopg
    with psycopg.connect(dsn) as conn:
        count, age = outbox_relay.measure_backlog(conn)
    assert count >= 3
    assert age >= 0.0

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DELETE FROM outbox WHERE aggregate_id = %s", (str(agg_id),))
