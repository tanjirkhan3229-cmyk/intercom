"""The ``sla-clock`` consumer (P1.7): outbox stream → SLA applied-state clock.

A dedicated process (mirrors ``reporting/consumer.py``) reading ``relay:outbox`` via its own
consumer group. Per conversation event it: auto-applies a matching rule-policy on
``conversation.created``; otherwise advances the applied SLA clock (satisfy response targets on an
agent reply, satisfy resolution on close, re-arm on reopen — :func:`sla.apply_conversation_event`).

Idempotency is DB-durable: each ``conversation_sla`` row stores ``last_seq`` (max per-aggregate
outbox ``seq`` folded); an event with ``seq <= last_seq`` is skipped. Single-instance via a
Postgres advisory lock — the fold is order-dependent, so a second consumer could reorder a
conversation's events and drop a satisfy. Entry point: ``relay sla-clock``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, NamedTuple

from redis.exceptions import ResponseError
from sqlalchemy import select, text

from relay.core.db import get_engine, session_scope
from relay.core.ids import IdPrefix, decode_public_id
from relay.core.logging import get_logger
from relay.core.outbox import OUTBOX_STREAM
from relay.core.redis import get_redis

from . import events, sla
from .models import ConversationSla, SlaPolicy

log = get_logger(__name__)

GROUP = "sla-clock"
CONSUMER = "sla-1"
CONV_TOPIC_PREFIX = "conversation."
BATCH_COUNT = 200
# Session-level advisory lock so only one clock consumer runs (ordered idempotent folds).
SLA_ADVISORY_LOCK = 0x0073_6C61_5F63_6C6B  # "sla_clk"


class ConsumeResult(NamedTuple):
    entries_read: int
    applied: int


async def ensure_group(redis: Any, *, group: str = GROUP) -> None:
    try:
        await redis.xgroup_create(OUTBOX_STREAM, group, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def _apply_to_db(topic: str, payload: dict[str, Any], seq: int) -> bool:
    """Fold one conversation event into its SLA clock. ``True`` if a row was created/changed."""
    ws_pub = payload.get("workspace_id")
    cnv_pub = payload.get("conversation_id")
    if not isinstance(ws_pub, str) or not isinstance(cnv_pub, str):
        return False
    workspace_id = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    conversation_id = decode_public_id(IdPrefix.CONVERSATION, cnv_pub)

    async with session_scope(workspace_id) as session:
        if topic == events.CONVERSATION_CREATED:
            return await sla.maybe_auto_apply(session, conversation_id)

        row = (
            await session.execute(
                select(ConversationSla)
                .where(ConversationSla.conversation_id == conversation_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            return False  # no SLA on this conversation
        if seq <= row.last_seq:
            return False  # already folded (idempotent replay)
        policy = await session.get(SlaPolicy, row.policy_id)
        if policy is None:  # policy deleted mid-flight — advance the watermark, nothing to fold
            row.last_seq = seq
            return False
        await sla.apply_conversation_event(session, row, policy, topic, payload)
        row.last_seq = seq
        return True


async def consume_once(
    redis: Any,
    *,
    group: str = GROUP,
    consumer: str = CONSUMER,
    from_id: str = ">",
    count: int = BATCH_COUNT,
    block_ms: int | None = None,
) -> ConsumeResult:
    """Read one batch and fold conversation events into the SLA clock (see
    ``reporting.consume_once`` for the ``from_id`` / ack / recovery semantics — identical)."""
    resp = await redis.xreadgroup(
        group, consumer, {OUTBOX_STREAM: from_id}, count=count, block=block_ms
    )
    if not resp:
        return ConsumeResult(entries_read=0, applied=0)
    entries_read = 0
    applied = 0
    for _stream, entries in resp:
        for entry_id, fields in entries:
            entries_read += 1
            topic = fields.get("topic", "")
            if topic.startswith(CONV_TOPIC_PREFIX):
                payload = json.loads(fields.get("payload") or "{}")
                seq = int(fields.get("seq") or 0)
                if await _apply_to_db(topic, payload, seq):
                    applied += 1
            await redis.xack(OUTBOX_STREAM, group, entry_id)
    return ConsumeResult(entries_read=entries_read, applied=applied)


async def run_sla_clock(block_ms: int = 5000) -> None:
    """Consume ``relay:outbox`` forever, advancing SLA clocks. Single-instance via advisory lock."""
    redis = get_redis()
    await ensure_group(redis)
    async with get_engine().connect() as lock_conn:
        got = (
            await lock_conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": SLA_ADVISORY_LOCK}
            )
        ).scalar_one()
        if not got:
            log.info("sla.clock.already_running")
            return
        # Crash recovery: drain the pending-entries list before taking new work.
        while (await consume_once(redis, from_id="0")).entries_read == BATCH_COUNT:
            pass
        log.info("sla.clock.started")
        while True:
            result = await consume_once(redis, from_id=">", block_ms=block_ms)
            if result.applied:
                log.info("sla.clock.applied", events=result.applied)


def main() -> None:
    asyncio.run(run_sla_clock())
