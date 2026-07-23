"""Celery tasks for the ``messaging`` module.

- ``ensure_partitions`` (queue ``housekeeping``) — pre-creates ``conversation_parts`` monthly
  partitions T+2 months ahead via ``relay_ensure_partitions`` and alerts on any still missing
  (mirrors the CRM ``events`` maintenance; each module owns its own partitioned tables).
- ``purge_idempotency_keys`` (queue ``housekeeping``) — deletes expired ``idempotency_keys``
  rows. Runs the ``relay_purge_expired_idempotency_keys`` SECURITY DEFINER function (owned by
  the BYPASSRLS ``migrator``, EXECUTE-granted to ``app_rw``) because a workspace-agnostic sweep
  can't run under RLS as ``app_rw``.

The outbox relay is *not* a Celery task — it's a dedicated long-running process
(``relay.core.outbox_relay.run_relay``, entry point ``relay outbox-relay``) using a session-mode
connection for LISTEN/NOTIFY + an advisory lock. Tasks are synchronous and idempotent.
"""

from __future__ import annotations

import psycopg

from relay.core.logging import get_logger
from relay.settings import get_settings
from relay.worker import celery_app

log = get_logger(__name__)

PARTITIONED_TABLES: tuple[str, ...] = ("conversation_parts",)
PARTITION_MONTHS_AHEAD = 2


@celery_app.task(name="messaging.ensure_partitions", queue="housekeeping")
def ensure_partitions(months_ahead: int = PARTITION_MONTHS_AHEAD) -> dict[str, list[str]]:
    """Pre-create ``conversation_parts`` partitions T+``months_ahead`` and alert if any missing."""
    dsn = get_settings().database_url_psycopg
    missing_by_table: dict[str, list[str]] = {}
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        for table in PARTITIONED_TABLES:
            cur.execute("SELECT relay_ensure_partitions(%s, %s)", (table, months_ahead))
            cur.execute("SELECT relay_missing_partitions(%s, %s)", (table, months_ahead))
            missing = [str(row[0]) for row in cur.fetchall()]
            if missing:
                log.error("messaging.partitions.missing", table=table, months=missing)
                missing_by_table[table] = missing
    return missing_by_table


@celery_app.task(name="messaging.purge_idempotency_keys", queue="housekeeping")
def purge_idempotency_keys() -> int:
    """Delete expired idempotency keys. Returns the number of rows removed. Idempotent."""
    dsn = get_settings().database_url_psycopg
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT relay_purge_expired_idempotency_keys()")
        row = cur.fetchone()
    deleted = int(row[0]) if row else 0
    if deleted:
        log.info("messaging.idempotency.purged", rows=deleted)
    return deleted
