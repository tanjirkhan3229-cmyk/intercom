"""Celery tasks for the ``integrations`` module (P1.9).

- ``integrations.slack_notify``          (queue ``send.channels``) — post a conversation
  notification to Slack. Bounded Celery retries on a transient Slack failure (best-effort v0
  notifications — no durable ledger; the durable path is webhooks/Zapier).
- ``integrations.slack_ingest_inbound``  (queue ``ingest``) — turn a signed Slack thread reply into
  a Relay admin reply (deduped on Slack's event_id).

Async service logic is driven on the persistent per-process loop via ``run_coro``.
"""

from __future__ import annotations

import random
import uuid
from typing import Any

from relay.core.asyncio_bridge import run_coro
from relay.core.logging import get_logger
from relay.worker import celery_app

from . import service

log = get_logger(__name__)

_MAX_BACKOFF = 300


def _backoff(retries: int) -> int:
    base = min(2**retries, _MAX_BACKOFF)
    return int(base * (0.5 + random.random()))  # 0.5x-1.5x jitter


@celery_app.task(name="integrations.slack_notify", queue="send.channels", bind=True, max_retries=5)
def slack_notify(self: Any, workspace_id: str, conversation_pub: str, topic: str, text: str) -> str:
    """Post one conversation notification to the workspace's Slack channel(s)."""
    try:
        return run_coro(
            service.deliver_slack_notification(
                uuid.UUID(workspace_id), conversation_pub, topic, text
            )
        )
    except service.SlackDeliveryError as exc:
        if self.request.retries >= self.max_retries:
            log.error("integrations.slack.exhausted", conversation=conversation_pub, error=str(exc))
            return "exhausted"
        raise self.retry(exc=exc, countdown=_backoff(self.request.retries)) from exc


@celery_app.task(name="integrations.slack_ingest_inbound", queue="ingest", bind=True, max_retries=5)
def slack_ingest_inbound(self: Any, workspace_id: str, event_json: str) -> str:
    """Post a Slack thread reply into the mapped Relay conversation (reply-from-Slack v0). Retries
    on a transient failure — ``ingest_slack_event`` releases its dedupe claim first, so the retry
    reprocesses cleanly rather than short-circuiting as a duplicate."""
    try:
        return run_coro(service.ingest_slack_event(uuid.UUID(workspace_id), event_json))
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            log.error("integrations.slack.ingest_exhausted", error=str(exc))
            return "failed"
        raise self.retry(exc=exc, countdown=_backoff(self.request.retries)) from exc
