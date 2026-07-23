"""Outbound email dispatcher (RFC-001 §6.5): outbox stream → ``send.email`` queue.

Agent email replies reach the wire the rule-compliant way: ``messaging.add_reply`` writes an
``outbox`` row in its transaction (never a direct enqueue, master rule 2); the outbox relay drains
it to the ``relay:outbox`` Redis stream; *this* consumer (its own group, mirroring
``core.realtime_fanout``) filters the email admin/ai replies and enqueues ``channels.send_email``.

At-least-once + a ``ch:email:done:{outbox_id}`` marker collapses the common redelivery to a single
enqueue. The true exactly-once guarantee for the *customer email* lives in the send task's DB gate
(``email_messages(workspace_id, part_id)``) — the marker is only an optimization.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from redis.exceptions import ResponseError

from relay.core.ids import IdPrefix, decode_public_id
from relay.core.logging import get_logger
from relay.core.outbox import OUTBOX_STREAM
from relay.core.redis import get_redis

log = get_logger(__name__)

GROUP = "channels-email"
CONSUMER = "channels-email-1"
PART_CREATED = "conversation.part.created"
_DEDUPE_PREFIX = "ch:email:done:"
_DEDUPE_TTL_SECONDS = 3600

# enqueue(workspace_id, conversation_id, part_id) — uuid strings.
Enqueue = Callable[[str, str, str], Awaitable[None] | None]


def _should_send(topic: str, payload: dict[str, Any]) -> bool:
    return (
        topic == PART_CREATED
        and payload.get("channel") == "email"
        and payload.get("author_kind") in ("admin", "ai_agent")
        and payload.get("part_type") == "comment"
    )


async def ensure_group(redis: Any, *, group: str = GROUP) -> None:
    try:
        await redis.xgroup_create(OUTBOX_STREAM, group, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def _already_done(redis: Any, outbox_id: str) -> bool:
    return bool(await redis.exists(f"{_DEDUPE_PREFIX}{outbox_id}"))


async def _mark_done(redis: Any, outbox_id: str) -> None:
    await redis.set(f"{_DEDUPE_PREFIX}{outbox_id}", "1", ex=_DEDUPE_TTL_SECONDS)


async def consume_once(
    redis: Any,
    enqueue: Enqueue,
    *,
    group: str = GROUP,
    consumer: str = CONSUMER,
    from_id: str = ">",
    count: int = 200,
    block_ms: int | None = None,
) -> int:
    """Read one batch and enqueue sends for fresh email replies. Returns the number enqueued.
    ``from_id='>'`` reads new entries; ``'0'`` re-reads this consumer's pending (crash recovery)."""
    resp = await redis.xreadgroup(
        group, consumer, {OUTBOX_STREAM: from_id}, count=count, block=block_ms
    )
    if not resp:
        return 0

    enqueued = 0
    for _stream, entries in resp:
        for entry_id, fields in entries:
            topic = fields.get("topic", "")
            payload = json.loads(fields.get("payload") or "{}")
            if _should_send(topic, payload):
                outbox_id = fields["outbox_id"]
                if not await _already_done(redis, outbox_id):
                    ws = str(decode_public_id(IdPrefix.WORKSPACE, payload["workspace_id"]))
                    conv = str(decode_public_id(IdPrefix.CONVERSATION, payload["conversation_id"]))
                    part = str(decode_public_id(IdPrefix.PART, payload["part_id"]))
                    result = enqueue(ws, conv, part)
                    if asyncio.iscoroutine(result):
                        await result
                    await _mark_done(redis, outbox_id)
                    enqueued += 1
            await redis.xack(OUTBOX_STREAM, group, entry_id)
    return enqueued


def _enqueue_celery(workspace_id: str, conversation_id: str, part_id: str) -> None:
    from .tasks import send_email

    send_email.apply_async(
        kwargs={
            "workspace_id": workspace_id,
            "conversation_id": conversation_id,
            "part_id": part_id,
        }
    )


async def run_dispatch(block_ms: int = 5000) -> None:
    """Consume ``relay:outbox`` forever and enqueue email sends. Entry: ``relay channels-dispatch``
    (its own process/compose service, like the realtime fan-out)."""
    redis = get_redis()
    await ensure_group(redis)
    # Crash recovery: drain delivered-but-un-acked entries first.
    while await consume_once(redis, _enqueue_celery, from_id="0") > 0:
        pass
    log.info("channels.dispatch.started")
    while True:
        n = await consume_once(redis, _enqueue_celery, from_id=">", block_ms=block_ms)
        if n:
            log.info("channels.dispatch.enqueued", sends=n)


def main() -> None:
    asyncio.run(run_dispatch())
