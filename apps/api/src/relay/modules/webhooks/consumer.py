"""Webhook dispatch consumer (P0.11; mirrors knowledge/revalidation.py).

Consumes ``relay:outbox`` and, for each event on a subscribable topic, finds the owning
workspace's *active* matching subscriptions and inserts one ``pending`` ``webhook_deliveries`` row
per (subscription, event), then enqueues ``webhooks.deliver`` for each. It sets the RLS GUC from
the payload's ``workspace_id`` before touching tenant tables (the stream is not tenant-scoped).

Crucially the consumer **never makes the outbound HTTP call** — that is the delivery task's job on
the ``webhooks`` Celery queue. So a slow or hung customer endpoint can never block this
single-instance stream drainer or delay other tenants (the P0.11 isolation guarantee).

At-least-once with a ``whk:dispatched:{outbox_id}`` dedupe marker (set only *after* enqueue, so a
crash before it redelivers rather than drops). Webhooks are at-least-once by contract; customers
dedupe on the event id.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid
from typing import Any

from redis.exceptions import ResponseError
from sqlalchemy import select

from relay.core.db import session_scope
from relay.core.ids import IdPrefix, decode_public_id, uuid7
from relay.core.logging import get_logger
from relay.core.outbox import OUTBOX_STREAM
from relay.core.redis import get_redis
from relay.worker import celery_app

from . import events
from .models import WebhookDelivery, WebhookSubscription

log = get_logger(__name__)

GROUP = "webhooks-dispatch"
CONSUMER = "dispatch-1"
_DEDUPE_PREFIX = "whk:dispatched:"
_DEDUPE_TTL_SECONDS = 3600


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def ensure_group(redis: Any, *, group: str = GROUP) -> None:
    try:
        await redis.xgroup_create(OUTBOX_STREAM, group, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def _enqueue(workspace_id: uuid.UUID, delivery_id: uuid.UUID, created_at_iso: str) -> None:
    celery_app.send_task(
        "webhooks.deliver",
        args=[str(workspace_id), str(delivery_id), created_at_iso],
        queue="webhooks",
    )


async def _create_deliveries(
    workspace_id: uuid.UUID, public_topic: str, outbox_id: uuid.UUID, payload: dict[str, Any]
) -> list[tuple[uuid.UUID, str]]:
    """Insert one pending delivery per active subscription matching ``public_topic``.

    Returns ``(delivery_id, created_at_iso)`` for each, enqueued after the txn commits.

    ``created_at`` is the **dispatch instant** (``now``). That is load-bearing: it is the RANGE
    partition key (so the row always lands in the current, seeded partition), the retention basis,
    AND the anchor for the 72h retry window (which must run from when delivery began, not from when
    the source event was created — a backlog-dispatched event must still get its full retry
    window). ``next_attempt_at`` is also now, so the retry scan is a durable backstop if the direct
    enqueue is lost.

    Delivery is **at-least-once**: the Redis ``whk:dispatched:`` marker collapses the common relay
    redelivery, and receivers dedupe on the stable ``Relay-Event-Id`` (= the outbox id) for the
    rarer cases the marker misses. (A per-dispatch ``created_at`` deliberately does NOT try to make
    the unique constraint an exactly-once key — coupling the dedup key to the event timestamp would
    break partition routing and the retry-window anchor.)
    """
    created: list[tuple[uuid.UUID, str]] = []
    now = _now()
    async with session_scope(workspace_id) as session:  # sets app.ws GUC
        subs = (
            await session.scalars(
                select(WebhookSubscription).where(
                    WebhookSubscription.status == "active",
                    WebhookSubscription.topics.contains([public_topic]),
                )
            )
        ).all()
        for sub in subs:
            did = uuid7()
            session.add(
                WebhookDelivery(
                    id=did,
                    workspace_id=workspace_id,
                    subscription_id=sub.id,
                    outbox_id=outbox_id,
                    topic=public_topic,
                    payload=payload,
                    attempt=0,
                    status="pending",
                    next_attempt_at=now,
                    created_at=now,
                )
            )
            created.append((did, now.isoformat()))
    return created


async def _handle_entry(redis: Any, group: str, entry_id: str, fields: dict[str, str]) -> bool:
    """Process one stream entry. Returns True iff fresh deliveries were dispatched.

    Never raises: a non-webhook topic or a malformed/poison entry is acked + skipped (so it can
    neither wedge the stream nor crash-loop); a retryable failure (DB/broker down) leaves the
    entry un-acked so a later pending-drain reprocesses it (at-least-once).
    """
    topic = fields.get("topic", "")
    if topic not in events.SUBSCRIBABLE_OUTBOX_TOPICS:
        await redis.xack(OUTBOX_STREAM, group, entry_id)
        return False
    try:
        outbox_id = uuid.UUID(fields["outbox_id"])
        payload = json.loads(fields.get("payload") or "{}")
        workspace_id = decode_public_id(IdPrefix.WORKSPACE, payload["workspace_id"])
    except (KeyError, ValueError) as exc:  # JSONDecodeError is a ValueError
        log.warning("webhooks.dispatch.skip_malformed", entry=str(entry_id), error=str(exc))
        await redis.xack(OUTBOX_STREAM, group, entry_id)
        return False
    if await redis.exists(f"{_DEDUPE_PREFIX}{outbox_id}"):
        await redis.xack(OUTBOX_STREAM, group, entry_id)
        return False
    public_topic = events.OUTBOX_TO_WEBHOOK_TOPIC[topic]
    try:
        created = await _create_deliveries(workspace_id, public_topic, outbox_id, payload)
        for delivery_id, created_iso in created:  # after commit → worker sees the row
            _enqueue(workspace_id, delivery_id, created_iso)
    except Exception as exc:  # DB/broker hiccup → retry (leave un-acked)
        log.warning("webhooks.dispatch.retry", entry=str(entry_id), error=str(exc))
        return False
    await redis.set(f"{_DEDUPE_PREFIX}{outbox_id}", "1", ex=_DEDUPE_TTL_SECONDS)
    await redis.xack(OUTBOX_STREAM, group, entry_id)
    return bool(created)


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
    """Consume ``relay:outbox`` forever and dispatch webhook deliveries. Entry point: ``relay
    webhook-dispatch`` (its own process, like the outbox relay / realtime fanout)."""
    redis = get_redis()
    await ensure_group(redis)
    while await consume_once(redis, from_id="0") > 0:  # crash recovery: drain pending first
        pass
    log.info("webhooks.dispatch.started")
    while True:
        n = await consume_once(redis, from_id=">", block_ms=block_ms)
        n += await consume_once(redis, from_id="0")  # retry anything left un-acked
        if n:
            log.info("webhooks.dispatch.dispatched", events=n)


def main() -> None:
    asyncio.run(run_dispatch())
