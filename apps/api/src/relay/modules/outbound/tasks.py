"""Celery tasks for the ``outbound`` module (P1.8).

Tasks are **sync** and drive the async service layer via ``core.asyncio_bridge.run_coro`` (one
persistent per-process loop; never ``asyncio.run`` per task). All are idempotent (master rule 3):

- ``fire_campaign``  (``housekeeping``) — snapshot the audience + pre-insert ``queued`` sends +
  enqueue ``send_chunk`` per 1k contacts. Resumable; a re-run repeats idempotent inserts.
- ``send_chunk``     (``send.email``)   — send each recipient via the exactly-once ``send_one``;
  one bad recipient never fails the chunk, a rate-limit retries the whole (idempotent) chunk.
- ``reconcile_stats``(``analytics``)    — recompute ``campaign_stats`` from the ledgers (±0.5% net).
- ``maintain_partitions`` (``housekeeping``, beat) — keep ``message_events`` partitions ahead + drop
  aged ones.
"""

from __future__ import annotations

import random
import uuid

import psycopg
from sqlalchemy import text

from relay.core.asyncio_bridge import run_coro
from relay.core.db import session_scope
from relay.core.errors import RateLimitedError
from relay.core.logging import get_logger
from relay.settings import get_settings
from relay.worker import celery_app

from . import service

log = get_logger(__name__)

_MAX_BACKOFF = 300
_MESSAGE_EVENTS_RETENTION_DAYS = 90


def _backoff(retries: int) -> int:
    base = min(2**retries, _MAX_BACKOFF)
    return int(base * (0.5 + random.random()))  # 0.5x-1.5x jitter


@celery_app.task(name="outbound.fire_campaign", queue="housekeeping", bind=True, max_retries=3)
def fire_campaign(self: object, workspace_id: str, campaign_id: str) -> str:
    ws = uuid.UUID(workspace_id)
    cid = uuid.UUID(campaign_id)

    def _enqueue(contact_ids: list[uuid.UUID]) -> None:
        celery_app.send_task(
            "outbound.send_chunk",
            args=[workspace_id, campaign_id, [str(c) for c in contact_ids]],
            queue="send.email",
        )

    try:
        return str(run_coro(service.run_fire_snapshot(ws, cid, enqueue=_enqueue)))
    except Exception as exc:
        if self.request.retries >= self.max_retries:  # type: ignore[attr-defined]
            log.error("outbound.fire_campaign.exhausted", campaign_id=campaign_id, error=str(exc))
            return "dlq"
        raise self.retry(exc=exc, countdown=_backoff(self.request.retries)) from exc  # type: ignore[attr-defined]


@celery_app.task(name="outbound.send_chunk", queue="send.email", bind=True, max_retries=5)
def send_chunk(
    self: object, workspace_id: str, campaign_id: str, contact_ids: list[str]
) -> dict[str, int]:
    ws = uuid.UUID(workspace_id)
    cid = uuid.UUID(campaign_id)
    sent = skipped = failed = 0
    rate_limited = False
    for raw in contact_ids:
        try:
            result = str(
                run_coro(
                    service.send_one(workspace_id=ws, campaign_id=cid, contact_id=uuid.UUID(raw))
                )
            )
        except RateLimitedError:
            # Provider/tenant rate exceeded — retry the whole chunk (already-sent rows are claimed,
            # so the re-run skips them). Break so we don't spin the rest at the same limit.
            rate_limited = True
            break
        except Exception as exc:
            log.error(
                "outbound.send_one.error", campaign_id=campaign_id, contact_id=raw, error=str(exc)
            )
            failed += 1
            continue
        if result == "sent":
            sent += 1
        elif result.startswith("skipped"):
            skipped += 1
        elif result.startswith("failed"):
            failed += 1
    if rate_limited:
        raise self.retry(countdown=_backoff(self.request.retries))  # type: ignore[attr-defined]
    return {"sent": sent, "skipped": skipped, "failed": failed}


@celery_app.task(name="outbound.fire_post", queue="housekeeping", bind=True, max_retries=3)
def fire_post(self: object, workspace_id: str, post_id: str) -> str:
    ws = uuid.UUID(workspace_id)
    pid = uuid.UUID(post_id)

    def _enqueue(contact_ids: list[uuid.UUID]) -> None:
        celery_app.send_task(
            "outbound.deliver_post_chunk",
            args=[workspace_id, post_id, [str(c) for c in contact_ids]],
            queue="send.channels",
        )

    try:
        return str(run_coro(service.run_post_snapshot(ws, pid, enqueue=_enqueue)))
    except Exception as exc:
        if self.request.retries >= self.max_retries:  # type: ignore[attr-defined]
            log.error("outbound.fire_post.exhausted", post_id=post_id, error=str(exc))
            return "dlq"
        raise self.retry(exc=exc, countdown=_backoff(self.request.retries)) from exc  # type: ignore[attr-defined]


@celery_app.task(
    name="outbound.deliver_post_chunk", queue="send.channels", bind=True, max_retries=5
)
def deliver_post_chunk(
    self: object, workspace_id: str, post_id: str, contact_ids: list[str]
) -> dict[str, int]:
    ws = uuid.UUID(workspace_id)
    pid = uuid.UUID(post_id)
    delivered = skipped = failed = 0
    for raw in contact_ids:
        try:
            result = str(
                run_coro(
                    service.deliver_post_receipt(
                        workspace_id=ws, post_id=pid, contact_id=uuid.UUID(raw)
                    )
                )
            )
        except Exception as exc:
            log.error(
                "outbound.deliver_post.error", post_id=post_id, contact_id=raw, error=str(exc)
            )
            failed += 1
            continue
        if result == "delivered":
            delivered += 1
        elif result.startswith("skipped"):
            skipped += 1
    return {"delivered": delivered, "skipped": skipped, "failed": failed}


@celery_app.task(name="outbound.reconcile_stats", queue="analytics")
def reconcile_stats(workspace_id: str, campaign_id: str) -> str:
    run_coro(service.reconcile_campaign_stats(uuid.UUID(workspace_id), uuid.UUID(campaign_id)))
    return "reconciled"


@celery_app.task(name="outbound.sweep_campaigns", queue="analytics")
def sweep_campaigns() -> int:
    """Periodic sweep (beat): reconcile firing-campaign stats from the ledgers and flip
    ``firing``→``sent`` for campaigns/posts with no pending work. Runs the SECURITY DEFINER
    ``relay_outbound_sweep`` (workspace-agnostic, all tenants) as ``app_rw``. Returns campaigns
    completed this run."""
    with (
        psycopg.connect(get_settings().database_url_psycopg, autocommit=True) as conn,
        conn.cursor() as cur,
    ):
        cur.execute("SELECT relay_outbound_sweep()")
        row = cur.fetchone()
    return int(row[0]) if row else 0


async def _maintain_partitions() -> None:
    async with session_scope(None) as session:  # global infra; SECURITY DEFINER funcs handle RLS
        await session.execute(text("SELECT relay_ensure_partitions('message_events', 2)"))
        await session.execute(
            text("SELECT relay_drop_old_partitions('message_events', :days)"),
            {"days": _MESSAGE_EVENTS_RETENTION_DAYS},
        )


@celery_app.task(name="outbound.maintain_partitions", queue="housekeeping")
def maintain_partitions() -> str:
    run_coro(_maintain_partitions())
    return "ok"
