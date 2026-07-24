"""The ``crm-segment-refresh`` consumer (P1.9): outbox stream → segment membership deltas.

A dedicated process reading the ``relay:outbox`` Redis stream via its **own** consumer group (like
reporting-metrics / webhooks-dispatch). On every ``crm.contact.created`` / ``crm.contact.updated``
it re-evaluates that one contact against all of the workspace's segments and adds/removes its
``segment_members`` rows — the real-time "delta path" (the P1.9 acceptance: membership converges
after an attribute flip, without waiting for the nightly reconcile).

Idempotency is level-triggered, not seq-based: ``refresh_contact_segments`` recomputes membership
truth from current DB state, so redelivery / full stream replay converge to the same rows (no
``last_seq`` bookkeeping needed). Single-instance via a Postgres advisory lock so the optimistic
``cached_member_count`` adjustments never race (the nightly reconcile is the authoritative count
regardless).

Feature flag: when ``segments_enabled`` is off the consumer still drains + acks the stream (so it
never becomes a backlog) but skips membership work — the nightly reconcile backfills later.

Entry point: ``relay segment-refresh``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, NamedTuple

from redis.exceptions import ResponseError
from sqlalchemy import text

from relay.core.db import get_engine, session_scope
from relay.core.ids import IdPrefix, decode_public_id
from relay.core.logging import get_logger
from relay.core.outbox import OUTBOX_STREAM
from relay.core.redis import get_redis
from relay.settings import get_settings

from . import service
from .events import CONTACT_CREATED, CONTACT_UPDATED

log = get_logger(__name__)

GROUP = "crm-segment-refresh"
CONSUMER = "segment-refresh-1"
_TOPICS = frozenset({CONTACT_CREATED, CONTACT_UPDATED})
# Entries read per batch; the recovery loop terminates when a read returns fewer (PEL drained).
BATCH_COUNT = 200
# Session-level advisory lock: single-instance so the optimistic count adjustments don't race.
SEGMENT_ADVISORY_LOCK = 0x0073_6567_7265_66  # "segref"


class ConsumeResult(NamedTuple):
    entries_read: int
    applied: int


async def ensure_group(redis: Any, *, group: str = GROUP) -> None:
    try:
        await redis.xgroup_create(OUTBOX_STREAM, group, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def _process(topic: str, payload: dict[str, Any]) -> bool:
    """Re-evaluate one contact's segment membership. ``True`` if any membership row changed."""
    ws_pub = payload.get("workspace_id")
    contact_pub = payload.get("contact_id")
    if not isinstance(ws_pub, str) or not isinstance(contact_pub, str):
        return False
    try:
        workspace_id = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
        contact_id = decode_public_id(IdPrefix.CONTACT, contact_pub)
    except ValueError:
        return False
    async with session_scope(workspace_id) as session:
        return (await service.refresh_contact_segments(session, contact_id)) > 0


async def consume_once(
    redis: Any,
    *,
    group: str = GROUP,
    consumer: str = CONSUMER,
    from_id: str = ">",
    count: int = BATCH_COUNT,
    block_ms: int | None = None,
) -> ConsumeResult:
    """Read one batch and apply contact events to segment membership.

    Every entry is acked (so a repeated ``'0'`` read walks the PEL to empty for crash recovery);
    non-contact topics and — when ``segments_enabled`` is off — all topics are acked-and-skipped.
    """
    resp = await redis.xreadgroup(
        group, consumer, {OUTBOX_STREAM: from_id}, count=count, block=block_ms
    )
    if not resp:
        return ConsumeResult(entries_read=0, applied=0)

    enabled = get_settings().segments_enabled
    entries_read = 0
    applied = 0
    for _stream, entries in resp:
        for entry_id, fields in entries:
            entries_read += 1
            topic = fields.get("topic", "")
            if enabled and topic in _TOPICS:
                try:
                    payload = json.loads(fields.get("payload") or "{}")
                except ValueError:
                    # A malformed payload must never wedge the consumer (it would abort the batch
                    # before ack → infinite redelivery). Ack + skip; nightly reconcile backstops.
                    log.warning("crm.segment_refresh.skip_malformed", entry=str(entry_id))
                    payload = None
                if payload is not None and await _process(topic, payload):
                    applied += 1
            await redis.xack(OUTBOX_STREAM, group, entry_id)
    return ConsumeResult(entries_read=entries_read, applied=applied)


async def run_consumer(block_ms: int = 5000) -> None:
    """Consume ``relay:outbox`` forever, projecting membership. Entry: ``relay segment-refresh``."""
    redis = get_redis()
    await ensure_group(redis)
    async with get_engine().connect() as lock_conn:
        got = (
            await lock_conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": SEGMENT_ADVISORY_LOCK}
            )
        ).scalar_one()
        if not got:
            log.info("crm.segment_refresh.already_running")
            return

        # Crash recovery: drain delivered-but-un-acked pending entries until the PEL is empty.
        while (await consume_once(redis, from_id="0")).entries_read == BATCH_COUNT:
            pass
        log.info("crm.segment_refresh.started")
        while True:
            result = await consume_once(redis, from_id=">", block_ms=block_ms)
            if result.applied:
                log.info("crm.segment_refresh.applied", segments_changed=result.applied)


def main() -> None:
    asyncio.run(run_consumer())
