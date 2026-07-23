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

import json
import uuid
from typing import Any

import psycopg

from relay.core.logging import get_logger
from relay.core.outbox import NOTIFY_CHANNEL, OUTBOX_STREAM
from relay.core.redis import get_redis_sync
from relay.settings import get_settings

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


def _publish_to_stream(redis: Any, rows: list[dict[str, Any]]) -> None:
    """XADD each row to the Redis stream. The ``outbox_id`` lets consumers dedupe redeliveries."""
    for row in rows:
        redis.xadd(
            OUTBOX_STREAM,
            {
                "outbox_id": str(row["id"]),
                "aggregate": row["aggregate"],
                "aggregate_id": str(row["aggregate_id"]),
                "seq": str(row["seq"]),
                "topic": row["topic"],
                "payload": json.dumps(row["payload"]),
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


def run_relay(poll_interval: float = 1.0, batch: int = RELAY_BATCH) -> None:
    """Run the relay loop forever: LISTEN-woken, poll every ``poll_interval`` as fallback.

    Single-instance via advisory lock (a second relay simply exits). Entry point:
    ``relay outbox-relay``. In dev/compose it runs as its own process alongside the workers.
    """
    dsn = get_settings().database_url_psycopg
    # Session-mode connections (no pooler transaction mode): LISTEN + advisory lock need them.
    with psycopg.connect(dsn, autocommit=True) as ctl:
        got = ctl.execute("SELECT pg_try_advisory_lock(%s)", (RELAY_ADVISORY_LOCK,)).fetchone()
        if not got or not got[0]:
            log.info("outbox.relay.already_running")
            return
        ctl.execute(f"LISTEN {NOTIFY_CHANNEL}")
        redis = get_redis_sync()
        log.info("outbox.relay.started")
        with psycopg.connect(dsn) as work:  # non-autocommit: publish txns commit here
            work.autocommit = False
            while True:
                published = drain(work, redis, batch)
                if published:
                    log.info("outbox.relay.published", rows=published)
                # Block until a NOTIFY arrives or the poll interval elapses (poll fallback).
                for _ in ctl.notifies(timeout=poll_interval, stop_after=1):
                    break
