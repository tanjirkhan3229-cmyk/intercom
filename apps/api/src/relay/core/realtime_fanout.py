"""Realtime-fanout consumer (RFC-001 §6.3, §6.5): outbox → Centrifugo.

The API never publishes realtime inline — it writes an ``outbox`` row in the domain transaction,
the outbox relay drains it to the ``relay:outbox`` Redis stream, and *this* process consumes the
stream and publishes conversation events to Centrifugo channels. That indirection is what makes
the whole path survive a Redis-pubsub or gateway outage: if publish fails or the gateway is down,
the entry stays un-acked and is retried; clients that were polling replay the gap from Postgres.

Delivery is **at-least-once, deduped to an exactly-once effect**:
- A Redis consumer group tracks position + redelivers un-acked entries after a crash.
- Publish happens *before* the done-marker is written, so a crash between them redelivers and
  republishes (never drops) — and clients dedupe by ``part_id`` (RFC-001 §6.3), the last line of
  defence against the rare double.
- A ``rt:fanout:done:{outbox_id}`` marker collapses the common redelivery to a single publish.

ponytail: single consumer instance (fixed consumer name). Horizontal scale-out needs distinct
consumer names per instance; the group already shards entries across them and the done-marker
keeps dedupe correct. Stream trimming is a housekeeping concern, out of P0.4 scope.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from redis.exceptions import ResponseError

from relay.core import realtime
from relay.core.logging import get_logger
from relay.core.outbox import OUTBOX_STREAM
from relay.core.redis import get_redis
from relay.settings import get_settings

log = get_logger(__name__)

GROUP = "realtime-fanout"
CONSUMER = "fanout-1"
CONV_TOPIC_PREFIX = "conversation."
POST_TOPIC_PREFIX = "outbound.post."  # in-app post delivery → the contact's own feed (P1.8)
_DEDUPE_PREFIX = "rt:fanout:done:"
_DEDUPE_TTL_SECONDS = 3600

Publisher = Callable[[str, dict[str, Any]], Awaitable[None]]


def channels_for_event(topic: str, payload: dict[str, Any]) -> list[str]:
    """Target channels for a conversation event: the conversation thread + two inbox buckets
    (the workspace firehose ``all`` and the team/unassigned bucket) so every inbox view updates."""
    # In-app post delivery goes to the recipient contact's own feed channel.
    if topic.startswith(POST_TOPIC_PREFIX):
        contact = payload.get("contact_id")
        return [realtime.contact_channel(contact)] if contact else []
    channels: list[str] = []
    conv = payload.get("conversation_id")
    ws = payload.get("workspace_id")
    if conv:
        channels.append(realtime.conv_channel(conv))
    if ws:
        team = payload.get("team_id") or realtime.INBOX_NONE
        channels.append(realtime.inbox_channel(ws, realtime.INBOX_ALL))
        channels.append(realtime.inbox_channel(ws, team))
    return channels


async def ensure_group(redis: Any, *, group: str = GROUP) -> None:
    try:
        # id="0" so a freshly-created group also picks up entries already on the stream.
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
    publish: Publisher,
    *,
    group: str = GROUP,
    consumer: str = CONSUMER,
    from_id: str = ">",
    count: int = 200,
    block_ms: int | None = None,
) -> int:
    """Read one batch and publish the fresh conversation events. Returns the number of *new*
    (not-yet-published) events fanned out. ``from_id=">"`` reads new entries; ``"0"`` re-reads
    this consumer's pending (un-acked) entries for crash recovery."""
    resp = await redis.xreadgroup(
        group, consumer, {OUTBOX_STREAM: from_id}, count=count, block=block_ms
    )
    if not resp:
        return 0

    published = 0
    for _stream, entries in resp:
        for entry_id, fields in entries:
            topic = fields.get("topic", "")
            if topic.startswith(CONV_TOPIC_PREFIX) or topic.startswith(POST_TOPIC_PREFIX):
                outbox_id = fields["outbox_id"]
                if not await _already_done(redis, outbox_id):
                    payload = json.loads(fields.get("payload") or "{}")
                    data = {"topic": topic, **payload}
                    for channel in channels_for_event(topic, payload):
                        await publish(channel, data)  # raises → entry left un-acked, retried
                    await _mark_done(redis, outbox_id)
                    published += 1
            await redis.xack(OUTBOX_STREAM, group, entry_id)
    return published


async def run_fanout(block_ms: int = 5000) -> None:
    """Consume ``relay:outbox`` forever and publish to Centrifugo. Entry point: ``relay
    realtime-fanout`` (its own process/compose service, like the outbox relay)."""
    redis = get_redis()
    await ensure_group(redis)
    async with httpx.AsyncClient(timeout=2.0) as client:

        async def _publish(channel: str, data: dict[str, Any]) -> None:
            await realtime.publish(channel, data, client=client)

        # Crash recovery: drain any pending (delivered-but-un-acked) entries first.
        while await consume_once(redis, _publish, from_id="0") > 0:
            pass
        log.info("realtime.fanout.started", api_url=get_settings().centrifugo_api_url)
        while True:
            n = await consume_once(redis, _publish, from_id=">", block_ms=block_ms)
            if n:
                log.info("realtime.fanout.published", events=n)


def main() -> None:
    asyncio.run(run_fanout())
