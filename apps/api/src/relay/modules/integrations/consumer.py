"""Slack notification dispatch consumer (P1.9; mirrors webhooks/consumer.py).

Consumes ``relay:outbox`` and, for each conversation event a workspace with an active Slack
integration cares about, enqueues an ``integrations.slack_notify`` task (the task makes the HTTP
call — the consumer never does, so a slow Slack API can't wedge this single-instance drainer). Only
**contact-authored** parts notify, so an admin reply that arrived from Slack is never echoed back
(the notify → reply → notify loop guard).

Entry point: ``relay slack-dispatch``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from redis.exceptions import ResponseError

from relay.core.db import session_scope
from relay.core.ids import IdPrefix, decode_public_id
from relay.core.logging import get_logger
from relay.core.outbox import OUTBOX_STREAM
from relay.core.redis import get_redis
from relay.settings import get_settings
from relay.worker import celery_app

from . import events, service, slack_format

log = get_logger(__name__)

GROUP = "integrations-slack-dispatch"
CONSUMER = "slack-dispatch-1"
_DEDUPE_PREFIX = "slk:notified:"
_DEDUPE_TTL_SECONDS = 3600


async def ensure_group(redis: Any, *, group: str = GROUP) -> None:
    try:
        await redis.xgroup_create(OUTBOX_STREAM, group, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def _enqueue(workspace_id: uuid.UUID, conversation_pub: str, topic: str, text: str) -> None:
    celery_app.send_task(
        "integrations.slack_notify",
        args=[str(workspace_id), conversation_pub, topic, text],
        queue="send.channels",
    )


async def _handle_entry(redis: Any, group: str, entry_id: str, fields: dict[str, str]) -> bool:
    """Process one stream entry. Returns True iff a notify task was enqueued. Never raises: a
    non-subject or malformed entry is acked + skipped; a retryable DB error leaves it un-acked."""
    topic = fields.get("topic", "")
    if not get_settings().slack_enabled or topic not in events.SUBSCRIBABLE_OUTBOX_TOPICS:
        await redis.xack(OUTBOX_STREAM, group, entry_id)
        return False
    try:
        outbox_id = fields["outbox_id"]
        payload = json.loads(fields.get("payload") or "{}")
        workspace_id = decode_public_id(IdPrefix.WORKSPACE, payload["workspace_id"])
        conversation_pub = payload["conversation_id"]
    except (KeyError, ValueError) as exc:
        log.warning("integrations.slack.skip_malformed", entry=str(entry_id), error=str(exc))
        await redis.xack(OUTBOX_STREAM, group, entry_id)
        return False

    # Loop guard: only contact-authored replies notify Slack (admin/ai replies do not).
    if topic == events.CONVERSATION_PART_CREATED and payload.get("author_kind") != "contact":
        await redis.xack(OUTBOX_STREAM, group, entry_id)
        return False
    if await redis.exists(f"{_DEDUPE_PREFIX}{outbox_id}"):
        await redis.xack(OUTBOX_STREAM, group, entry_id)
        return False

    try:
        async with session_scope(workspace_id) as session:
            has_slack = await service.has_active_slack(session)
    except Exception as exc:  # DB hiccup → retry (leave un-acked)
        log.warning("integrations.slack.retry", entry=str(entry_id), error=str(exc))
        return False

    enqueued = False
    if has_slack:
        _enqueue(
            workspace_id, conversation_pub, topic, slack_format.format_notification(topic, payload)
        )
        enqueued = True
    await redis.set(f"{_DEDUPE_PREFIX}{outbox_id}", "1", ex=_DEDUPE_TTL_SECONDS)
    await redis.xack(OUTBOX_STREAM, group, entry_id)
    return enqueued


async def consume_once(
    redis: Any,
    *,
    group: str = GROUP,
    consumer: str = CONSUMER,
    from_id: str = ">",
    count: int = 200,
    block_ms: int | None = None,
) -> int:
    resp = await redis.xreadgroup(
        group, consumer, {OUTBOX_STREAM: from_id}, count=count, block=block_ms
    )
    if not resp:
        return 0
    handled = 0
    for _stream, entries in resp:
        for entry_id, fields in entries:
            if await _handle_entry(redis, group, entry_id, fields):
                handled += 1
    return handled


async def run_dispatch(block_ms: int = 5000) -> None:
    """Consume ``relay:outbox`` forever, enqueuing Slack notifications (slack-dispatch)."""
    redis = get_redis()
    await ensure_group(redis)
    while await consume_once(redis, from_id="0") > 0:  # crash recovery: drain pending first
        pass
    log.info("integrations.slack.started")
    while True:
        n = await consume_once(redis, from_id=">", block_ms=block_ms)
        if n:
            log.info("integrations.slack.enqueued", count=n)


def main() -> None:
    asyncio.run(run_dispatch())
