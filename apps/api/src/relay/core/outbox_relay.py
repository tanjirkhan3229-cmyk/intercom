"""Outbox relay (RFC-001 §6.5): drains ``outbox`` to Redis, at-least-once, per-aggregate order.

LISTEN/NOTIFY-woken with a poll fallback. Each pending row is published to the ``relay:outbox``
Redis stream (durable) and then deleted. **Crash-safety / at-least-once:** a row is deleted only
*after* its stream publish has committed, so a crash between publish and delete merely redelivers
it — consumers dedupe by the ``outbox_id`` carried in every stream entry. Published rows are
deleted aggressively so the table stays small and hot (RFC-002 §5.6).

Ordering: rows are drained ``ORDER BY aggregate_id, seq`` so each aggregate's events reach the
stream in sequence. A single relay instance is enforced with a Postgres advisory lock; the
connection is session-mode (LISTEN + advisory lock need it — RFC-002 §9).

Sync psycopg + sync Redis, like the analytics drain (Celery/worker context is synchronous).
``publish_pending`` / the fetch+publish+delete helpers are separable so the chaos test can
interrupt a batch between publish and delete and prove at-least-once + dedupe.
"""

from __future__ import annotations

import contextlib
import json
import random
import time
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import redis.exceptions

from relay.core.logging import get_logger
from relay.core.observability.tracing import TRACE_CARRIER_KEY
from relay.core.outbox import NOTIFY_CHANNEL, OUTBOX_STREAM
from relay.core.redis import get_redis_sync
from relay.settings import get_settings

# Connectivity errors the relay recovers from by reconnecting with backoff (RFC-001 §9). psycopg's
# InterfaceError is a SIBLING of OperationalError (not a subclass), and a Redis blip raises redis
# errors that are unrelated to psycopg — all of them must be survived, not fatal.
_TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    psycopg.OperationalError,
    psycopg.InterfaceError,
    redis.exceptions.ConnectionError,
    redis.exceptions.TimeoutError,
)

log = get_logger(__name__)

# One advisory-lock key so only one relay drains at a time (arbitrary constant).
RELAY_ADVISORY_LOCK = 0x0075_7462_6F78  # "utbox"
# Rows per drain pass. Small: the relay loops until empty, so this only bounds a single txn.
RELAY_BATCH = 500

_FETCH_SQL = (
    "SELECT id, aggregate, aggregate_id, seq, topic, payload "
    "FROM outbox WHERE published_at IS NULL "
    "ORDER BY aggregate_id, seq, id LIMIT %s"
)
_DELETE_SQL = "DELETE FROM outbox WHERE id = ANY(%s)"
# Queue-depth + oldest-message-age (RFC-001 §9 alerts). Rows are deleted on publish, so pending =
# everything still present; ``age`` is 0 when the outbox is empty.
_BACKLOG_SQL = (
    "SELECT count(*), COALESCE(EXTRACT(EPOCH FROM now() - min(created_at)), 0) "
    "FROM outbox WHERE published_at IS NULL"
)


def measure_backlog(conn: psycopg.Connection) -> tuple[int, float]:
    """Return ``(pending_rows, oldest_age_seconds)`` for the outbox — the two alertable signals."""
    row = conn.execute(_BACKLOG_SQL).fetchone()
    if row is None:
        return 0, 0.0
    return int(row[0]), float(row[1])


def _record_backlog(conn: psycopg.Connection) -> None:
    """Set the backlog gauges. Best-effort — metrics must never break the relay."""
    try:
        from relay.core.observability.metrics import OUTBOX_OLDEST_AGE, OUTBOX_PENDING

        count, age = measure_backlog(conn)
        OUTBOX_PENDING.set(count)
        OUTBOX_OLDEST_AGE.set(age)
    except Exception:  # pragma: no cover - defensive
        pass


def _fetch_pending(conn: psycopg.Connection, limit: int) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(_FETCH_SQL, (limit,))
        rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "aggregate": r[1],
            "aggregate_id": r[2],
            "seq": r[3],
            "topic": r[4],
            "payload": r[5],
        }
        for r in rows
    ]


@contextlib.contextmanager
def _publish_span(carrier: dict[str, str] | None, topic: str) -> Iterator[None]:
    """Parent a publish span to the request that emitted the row (RFC-001 §6.5). A no-op when
    there is no carrier (tracing off) or if OTel isn't importable — never breaks a publish."""
    if not carrier:
        yield
        return
    try:
        from opentelemetry import trace

        from relay.core.observability.tracing import extract_context

        tracer = trace.get_tracer("relay.outbox_relay")
        span_ctx = extract_context(carrier)
        with tracer.start_as_current_span(f"outbox.publish {topic}", context=span_ctx):
            yield
    except Exception:  # pragma: no cover - tracing must never break delivery
        yield


def _publish_to_stream(redis: Any, rows: list[dict[str, Any]]) -> None:
    """XADD each row to the Redis stream. The ``outbox_id`` lets consumers dedupe redeliveries.

    The internal trace carrier (``_trace``) is stripped from the payload here so it never reaches
    downstream consumers or clients; it is used only to parent the publish span.
    """
    for row in rows:
        payload = row["payload"]
        carrier: dict[str, str] | None = None
        if isinstance(payload, dict) and TRACE_CARRIER_KEY in payload:
            carrier = payload.get(TRACE_CARRIER_KEY)
            payload = {k: v for k, v in payload.items() if k != TRACE_CARRIER_KEY}
        with _publish_span(carrier, row["topic"]):
            redis.xadd(
                OUTBOX_STREAM,
                {
                    "outbox_id": str(row["id"]),
                    "aggregate": row["aggregate"],
                    "aggregate_id": str(row["aggregate_id"]),
                    "seq": str(row["seq"]),
                    "topic": row["topic"],
                    "payload": json.dumps(payload),
                },
            )


def _delete_published(conn: psycopg.Connection, ids: list[uuid.UUID]) -> None:
    with conn.cursor() as cur:
        cur.execute(_DELETE_SQL, (ids,))


def publish_pending(conn: psycopg.Connection, redis: Any, limit: int = RELAY_BATCH) -> int:
    """Drain one batch: fetch → publish to Redis → delete → commit. Returns rows published.

    The publish happens before the delete/commit, so a failure after publish leaves the rows
    in place to be redelivered (at-least-once). ``conn`` must be non-autocommit.
    """
    rows = _fetch_pending(conn, limit)
    if not rows:
        return 0
    _publish_to_stream(redis, rows)
    _delete_published(conn, [r["id"] for r in rows])
    conn.commit()
    return len(rows)


def drain(conn: psycopg.Connection, redis: Any, batch: int = RELAY_BATCH) -> int:
    """Publish every pending row (loop until empty). Returns total rows published."""
    total = 0
    while True:
        n = publish_pending(conn, redis, batch)
        if n == 0:
            return total
        total += n


# Reconnect backoff bounds for connectivity loss (Postgres failover / Redis blip — RFC-001 §9).
_RECONNECT_BACKOFF_START = 1.0
_RECONNECT_BACKOFF_MAX = 30.0


def _sleep_backoff(backoff: float) -> float:
    """Sleep with equal-jitter (master rule 5: bounded + jittered) and return the next backoff."""
    time.sleep(backoff * (0.5 + random.random() * 0.5))
    return min(backoff * 2, _RECONNECT_BACKOFF_MAX)


def run_relay(poll_interval: float = 1.0, batch: int = RELAY_BATCH) -> None:
    """Run the relay loop forever, surviving Postgres failover and Redis blips (RFC-001 §9).

    LISTEN-woken with a poll fallback; single-instance via a session advisory lock. On a
    connectivity loss (writer failover ≈30 s, pooler teardown, or a Redis hiccup) it reconnects
    with capped, jittered backoff instead of dying — the advisory lock auto-releases when the
    session dies, and at-least-once still holds because unpublished rows are never deleted.

    Advisory-lock contention: on the FIRST attempt a held lock means a genuine peer is running, so
    we exit cleanly. After we have held the lock at least once, a failed re-acquire during a
    reconnect is almost always our own not-yet-reaped session (ghost) after a connection drop — we
    retry with backoff rather than exiting, so the outbox never silently stops draining. Entry
    point: ``relay outbox-relay``.
    """
    dsn = get_settings().database_url_psycopg
    metrics_on = get_settings().metrics_enabled
    if metrics_on:
        from relay.core.observability.metrics import start_metrics_server

        start_metrics_server()

    backoff = _RECONNECT_BACKOFF_START
    acquired_once = False
    while True:
        try:
            # Session-mode connections (no pooler txn mode): LISTEN + advisory lock need them.
            with psycopg.connect(dsn, autocommit=True) as ctl:
                got = ctl.execute(
                    "SELECT pg_try_advisory_lock(%s)", (RELAY_ADVISORY_LOCK,)
                ).fetchone()
                if not got or not got[0]:
                    if not acquired_once:
                        log.info("outbox.relay.already_running")  # genuine peer → step aside
                        return
                    # We ran before; the lock is likely our own ghost still being reaped.
                    log.warning("outbox.relay.lock_contended", backoff_s=backoff)
                    backoff = _sleep_backoff(backoff)
                    continue
                acquired_once = True
                ctl.execute(f"LISTEN {NOTIFY_CHANNEL}")
                redis = get_redis_sync()
                log.info("outbox.relay.started")
                with psycopg.connect(dsn) as work:  # non-autocommit: publish txns commit here
                    work.autocommit = False
                    while True:
                        # Sample queue-depth/oldest-age on the autocommit control conn (doesn't
                        # hold a snapshot open on the work conn) before draining.
                        if metrics_on:
                            _record_backlog(ctl)
                        published = drain(work, redis, batch)
                        if published:
                            log.info("outbox.relay.published", rows=published)
                            if metrics_on:
                                _record_backlog(ctl)  # reflect the drained-to-empty state
                        # Block until a NOTIFY arrives or the poll interval elapses (poll fallback).
                        for _ in ctl.notifies(timeout=poll_interval, stop_after=1):
                            break
                        # Reset backoff only after a full healthy cycle (work conn + drain +
                        # notify all succeeded), so a partial failover doesn't pin backoff at 1s.
                        backoff = _RECONNECT_BACKOFF_START
        except _TRANSIENT_ERRORS as exc:
            # Postgres or Redis connectivity dropped. Reconnect with jittered backoff, don't die.
            log.warning("outbox.relay.reconnect", error=str(exc), backoff_s=backoff)
            backoff = _sleep_backoff(backoff)
