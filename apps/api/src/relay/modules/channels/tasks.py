"""Celery tasks for the ``channels`` module (P0.7 email).

Tasks are **sync** (the Celery worker is synchronous) and drive the async service layer through
``core.asyncio_bridge.run_coro`` — one persistent per-process event loop, so the global asyncpg
engine is reused across tasks (never ``asyncio.run`` per task; see asyncio_bridge). Every task is
idempotent (master rule 3):

- ``ingest_email``  (queue ``ingest``)    — parse + route + append one inbound email; permanent
  failures (malformed / unroutable) go to the DLQ log and ACK (never re-raise → no poison loop),
  transient failures retry with bounded/jittered backoff, then DLQ.
- ``send_email``    (queue ``send.email``)— deliver an agent reply; suppression/size are terminal,
  provider/rate failures retry.
- ``record_ses_event`` (queue ``webhooks``) — SES bounce/complaint → suppression.
- ``poll_domains``  (queue ``housekeeping``) — verify pending sending domains.
"""

from __future__ import annotations

import random
import uuid

from relay.core.asyncio_bridge import run_coro
from relay.core.db import session_scope
from relay.core.logging import get_logger
from relay.worker import celery_app

from . import service
from .models import EmailDeliveryEvent, IngestFailure

log = get_logger(__name__)

_MAX_BACKOFF = 300


def _backoff(retries: int) -> int:
    """Exponential backoff with jitter (bounded), for transient-failure retries."""
    base = min(2**retries, _MAX_BACKOFF)
    return int(base * (0.5 + random.random()))  # 0.5x-1.5x jitter


async def _record_failure(*, sns_message_id: str, s3_bucket: str, s3_key: str, error: str) -> None:
    async with session_scope(None) as session:  # global infra table, no RLS
        session.add(
            IngestFailure(
                sns_message_id=sns_message_id,
                s3_bucket=s3_bucket,
                s3_key=s3_key,
                error=error[:2000],
            )
        )
        await session.flush()


async def _record_send_failure(*, workspace_id: str, part_id: str, error: str) -> None:
    """Record a 'failed' delivery event when a send exhausts its retries (DLQ signal)."""
    ws = uuid.UUID(workspace_id)
    async with session_scope(ws) as session:
        session.add(
            EmailDeliveryEvent(
                workspace_id=ws,
                part_id=uuid.UUID(part_id),
                email=None,
                event="failed",
                detail={"error": error[:2000]},
            )
        )
        await session.flush()


@celery_app.task(name="channels.ingest_email", queue="ingest", bind=True, max_retries=5)
def ingest_email(  # type: ignore[no-untyped-def]
    self,
    sns_message_id: str,
    s3_bucket: str,
    s3_key: str,
    recipients: list[str] | None = None,
) -> str:
    try:
        return run_coro(
            service.ingest(
                sns_message_id=sns_message_id,
                s3_bucket=s3_bucket,
                s3_key=s3_key,
                recipients=recipients,
            )
        )
    except service.UnroutableEmail as exc:
        # Permanent: DLQ + alert + ACK (do NOT re-raise — acks_late would redeliver forever).
        run_coro(
            _record_failure(
                sns_message_id=sns_message_id, s3_bucket=s3_bucket, s3_key=s3_key, error=str(exc)
            )
        )
        log.error("channels.ingest.unroutable", sns_message_id=sns_message_id, error=str(exc))
        return "dlq"
    except Exception as exc:  # transient (S3/DB blip) → bounded retry, then DLQ
        if self.request.retries >= self.max_retries:
            run_coro(
                _record_failure(
                    sns_message_id=sns_message_id,
                    s3_bucket=s3_bucket,
                    s3_key=s3_key,
                    error=f"exhausted retries: {exc}",
                )
            )
            log.error("channels.ingest.exhausted", sns_message_id=sns_message_id, error=str(exc))
            return "dlq"
        raise self.retry(exc=exc, countdown=_backoff(self.request.retries)) from exc


@celery_app.task(name="channels.send_email", queue="send.email", bind=True, max_retries=8)
def send_email(self, workspace_id: str, conversation_id: str, part_id: str) -> str:  # type: ignore[no-untyped-def]
    try:
        return run_coro(
            service.send_email(
                workspace_id=uuid.UUID(workspace_id),
                conversation_id=uuid.UUID(conversation_id),
                part_id=uuid.UUID(part_id),
            )
        )
    except service.SuppressedRecipient:
        # Terminal: the service already recorded the 'blocked' delivery event; ack.
        log.info("channels.send.suppressed", part_id=part_id)
        return "blocked_suppressed"
    except service.MessageTooLarge:
        log.error("channels.send.too_large", part_id=part_id)
        return "blocked_too_large"
    except Exception as exc:
        # Transient (provider SendError, rate limit, DB/Redis blip): a reply is must-not-lose, so
        # retry with bounded backoff, then DLQ (record + alert) rather than acking it into the void.
        if self.request.retries >= self.max_retries:
            run_coro(
                _record_send_failure(workspace_id=workspace_id, part_id=part_id, error=str(exc))
            )
            log.error("channels.send.exhausted", part_id=part_id, error=str(exc))
            return "dlq"
        raise self.retry(exc=exc, countdown=_backoff(self.request.retries)) from exc


@celery_app.task(name="channels.record_ses_event", queue="webhooks", bind=True, max_retries=5)
def record_ses_event(self, message_json: str) -> str:  # type: ignore[no-untyped-def]
    try:
        return run_coro(service.record_ses_event(message_json=message_json))
    except Exception as exc:  # transient → retry
        raise self.retry(exc=exc, countdown=_backoff(self.request.retries)) from exc


@celery_app.task(name="channels.poll_domains", queue="housekeeping")
def poll_domains() -> int:
    return run_coro(service.poll_pending_domains())
