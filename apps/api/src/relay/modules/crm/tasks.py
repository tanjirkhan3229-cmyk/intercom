"""Celery tasks for the ``crm`` module (RFC-002 §5.4).

- ``drain_events``      (queue ``analytics``) — drains the per-workspace Redis event buffers
  into the partitioned ``events`` table. Because PostgreSQL forbids ``COPY FROM`` on an
  RLS-enabled table, each chunk lands via a session ``TEMP`` stage (no RLS) then
  ``INSERT … SELECT`` through the parent under ``app.ws`` — one transaction per chunk, with
  the RLS WITH CHECK enforcing tenant isolation on the write path too. At-least-once: a
  chunk is moved to a per-workspace *processing* list (``LMOVE``) before the COPY and only
  deleted after commit; a crashed run recovers leftovers on the next pass (dedup is
  downstream per W3).
- ``ensure_partitions`` (queue ``housekeeping``) — pre-creates monthly partitions T+2 months
  ahead via the ``relay_ensure_partitions`` SECURITY DEFINER function and alerts (error log)
  if any expected partition is still missing.

Tasks are synchronous (Celery workers run sync); they use raw ``psycopg`` + sync Redis.
Every task is idempotent (master rule 3).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import psycopg

from relay.core.logging import get_logger
from relay.core.redis import get_redis_sync
from relay.settings import get_settings
from relay.worker import celery_app

from .service import EVENTS_BUFFER_WORKSPACES, events_buffer_key

log = get_logger(__name__)

# Rows per COPY transaction. Sized so a 10k batch lands in a handful of chunks (W3).
DRAIN_CHUNK = 5_000

# Partitioned tables the housekeeping task keeps ahead of the calendar. Extended as
# monthly-partitioned tables land (conversation_parts/sends/message_events in later phases).
PARTITIONED_TABLES: tuple[str, ...] = ("events",)
PARTITION_MONTHS_AHEAD = 2

_STAGE_DDL = (
    "CREATE TEMP TABLE _ev_stage ("
    "workspace_id uuid, contact_id uuid, name text, properties jsonb, created_at timestamptz"
    ") ON COMMIT DROP"
)
_COPY_SQL = "COPY _ev_stage (workspace_id, contact_id, name, properties, created_at) FROM STDIN"
_INSERT_SQL = (
    "INSERT INTO events (workspace_id, contact_id, name, properties, created_at) "
    "SELECT workspace_id, contact_id, name, properties, created_at FROM _ev_stage"
)


def _processing_key(buffer_key: str) -> str:
    return f"{buffer_key}:processing"


def _recover_processing(redis: Any, src: str, proc: str) -> None:
    """Return any items stranded in a processing list (crash mid-chunk) back to the source."""
    while redis.lmove(proc, src, "LEFT", "RIGHT") is not None:
        pass


def _take_chunk(redis: Any, src: str, proc: str, size: int) -> list[str]:
    """Reliably move up to ``size`` items from ``src`` to ``proc`` (crash-safe handoff)."""
    pipe = redis.pipeline(transaction=False)
    for _ in range(size):
        pipe.lmove(src, proc, "LEFT", "RIGHT")
    return [v for v in pipe.execute() if v is not None]


def _copy_chunk(conn: psycopg.Connection, workspace_id: str, rows: list[str]) -> None:
    """Land one chunk in a single transaction: temp-stage COPY + INSERT…SELECT under RLS."""
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.ws', %s, true)", (workspace_id,))
        cur.execute(_STAGE_DDL)
        with cur.copy(_COPY_SQL) as copy:
            for raw in rows:
                e = json.loads(raw)
                copy.write_row(
                    (
                        e["workspace_id"],
                        e["contact_id"],
                        e["name"],
                        json.dumps(e.get("properties", {})),
                        e["created_at"],
                    )
                )
        cur.execute(_INSERT_SQL)
    conn.commit()


def _drain_workspace(redis: Any, workspace_id: str) -> int:
    src = events_buffer_key(uuid.UUID(workspace_id))
    proc = _processing_key(src)
    _recover_processing(redis, src, proc)

    drained = 0
    dsn = get_settings().database_url_psycopg
    with psycopg.connect(dsn) as conn:
        conn.autocommit = False
        while True:
            rows = _take_chunk(redis, src, proc, DRAIN_CHUNK)
            if not rows:
                break
            _copy_chunk(conn, workspace_id, rows)
            redis.delete(proc)
            drained += len(rows)

    # Deregister only when fully drained (guards a concurrent producer).
    if redis.llen(src) == 0:
        redis.srem(EVENTS_BUFFER_WORKSPACES, workspace_id)
    if drained:
        log.info("crm.events.drained", workspace_id=workspace_id, rows=drained)
    return drained


@celery_app.task(name="crm.drain_events", queue="analytics")
def drain_events() -> int:
    """Drain every workspace's event buffer into ``events``. Returns rows landed."""
    redis = get_redis_sync()
    total = 0
    for workspace_id in list(redis.smembers(EVENTS_BUFFER_WORKSPACES)):
        total += _drain_workspace(redis, str(workspace_id))
    return total


@celery_app.task(name="crm.ensure_partitions", queue="housekeeping")
def ensure_partitions(months_ahead: int = PARTITION_MONTHS_AHEAD) -> dict[str, list[str]]:
    """Pre-create monthly partitions T+``months_ahead`` and alert on any still missing.

    Partition DDL runs via ``relay_ensure_partitions`` (SECURITY DEFINER, owned by the
    BYPASSRLS ``migrator``, EXECUTE-granted to ``app_rw``) so the app role never needs DDL.
    """
    dsn = get_settings().database_url_psycopg
    missing_by_table: dict[str, list[str]] = {}
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        for table in PARTITIONED_TABLES:
            cur.execute("SELECT relay_ensure_partitions(%s, %s)", (table, months_ahead))
            cur.execute("SELECT relay_missing_partitions(%s, %s)", (table, months_ahead))
            missing = [str(row[0]) for row in cur.fetchall()]
            if missing:
                # Alert hook: a missing partition means inserts would fail — page on this.
                log.error("crm.partitions.missing", table=table, months=missing)
                missing_by_table[table] = missing
    return missing_by_table
