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

import random
import uuid
from typing import Any

import psycopg

from relay.core.asyncio_bridge import run_coro
from relay.core.logging import get_logger
from relay.settings import get_settings
from relay.worker import celery_app

from . import push_service

log = get_logger(__name__)

PARTITIONED_TABLES: tuple[str, ...] = ("conversation_parts",)
PARTITION_MONTHS_AHEAD = 2

_MAX_BACKOFF = 300


def _backoff(retries: int) -> int:
    """Exponential backoff with jitter (bounded), for transient push-failure retries."""
    base = min(2**retries, _MAX_BACKOFF)
    return int(base * (0.5 + random.random()))  # 0.5x-1.5x jitter


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


@celery_app.task(name="messaging.send_push", queue="send.channels", bind=True, max_retries=3)
def send_push(self: Any, workspace_id: str, conversation_id: str, part_id: str) -> str:
    """Fan an agent/AI reply out to the contact's mobile devices (P1.10). Push is best-effort —
    the message is already persisted — so transient failures retry a bounded number of times and
    then give up rather than DLQ. Dead tokens are retired in ``push_service`` (never retried)."""
    try:
        n = run_coro(
            push_service.fanout_push_for_part(
                workspace_id=uuid.UUID(workspace_id),
                conversation_id=uuid.UUID(conversation_id),
                part_id=uuid.UUID(part_id),
            )
        )
        return f"pushed:{n}"
    except Exception as exc:  # transient (provider/breaker/DB blip) → bounded retry, then give up
        if self.request.retries >= self.max_retries:
            log.error("messaging.push.exhausted", part_id=part_id, error=str(exc))
            return "gave_up"
        raise self.retry(exc=exc, countdown=_backoff(self.request.retries)) from exc
