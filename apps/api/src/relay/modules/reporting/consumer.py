"""The ``reporting-metrics`` consumer (RFC-001 §6.3/§6.5): outbox stream → ``conversation_metrics``.

Like the realtime-fanout consumer, this is a dedicated process reading the ``relay:outbox`` Redis
stream via its **own** consumer group (independent of fan-out), so it sees every conversation event
exactly once per group. It is a thin shell around the pure ``reducer`` (``reducer.apply_event``):
per event it loads the conversation's metrics row, folds the event in, and upserts.

Idempotency is DB-durable, not TTL-based: each row stores ``last_seq`` (the max per-aggregate outbox
``seq`` applied), and an event whose ``seq <= last_seq`` is skipped. Because the outbox relay drains
in ``(aggregate_id, seq)`` order and the stream preserves it, events for a conversation arrive in
order — so at-least-once redelivery and full stream replay both converge to the same row. The
consumer never reads ``conversation_parts`` (P0.9 acceptance): everything comes from the event
payload (``occurred_at``, part ``created_at``, ``rating``).

Entry point: ``relay reporting-metrics`` (its own process/compose service).
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
from relay.modules.reporting.models import ConversationMetric
from relay.modules.reporting.reducer import Metrics, apply_event

log = get_logger(__name__)

GROUP = "reporting-metrics"
CONSUMER = "reporting-1"
CONV_TOPIC_PREFIX = "conversation."
# Entries read per batch; the recovery loop terminates when a read returns fewer (PEL drained).
BATCH_COUNT = 200
# Session-level Postgres advisory lock so only one metrics consumer runs at a time (single-instance
# is required for ordered idempotent folds — see run_metrics). Distinct from the outbox relay's key.
REPORTING_ADVISORY_LOCK = 0x0072_6570_6F72_74  # "report"


class ConsumeResult(NamedTuple):
    """One batch's outcome. ``entries_read`` drives crash-recovery termination (drain the PEL);
    ``applied`` is how many events actually changed a row (idempotent no-ops don't count)."""

    entries_read: int
    applied: int


# Columns the reducer owns (everything but identity/audit). Kept in one place for row<->Metrics.
_METRIC_FIELDS = (
    "team_id",
    "assignee_id",
    "opened_at",
    "first_admin_reply_at",
    "first_response_s",
    "closed_at",
    "resolution_s",
    "reopen_count",
    "replies_count",
    "rating",
    "rated_at",
    "ai_involved",
    "last_seq",
)


def _metrics_from_row(row: ConversationMetric) -> Metrics:
    return Metrics(
        workspace_id=row.workspace_id,
        conversation_id=row.conversation_id,
        **{name: getattr(row, name) for name in _METRIC_FIELDS},
    )


async def ensure_group(redis: Any, *, group: str = GROUP) -> None:
    try:
        await redis.xgroup_create(OUTBOX_STREAM, group, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def _apply_to_db(topic: str, payload: dict[str, Any], seq: int) -> bool:
    """Fold one event into the conversation's metrics row. ``True`` if a row was written."""
    ws_pub = payload.get("workspace_id")
    cnv_pub = payload.get("conversation_id")
    if not isinstance(ws_pub, str) or not isinstance(cnv_pub, str):
        return False
    workspace_id = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    conversation_id = decode_public_id(IdPrefix.CONVERSATION, cnv_pub)

    async with session_scope(workspace_id) as session:
        row = (
            await session.execute(
                select(ConversationMetric)
                .where(ConversationMetric.conversation_id == conversation_id)
                .with_for_update()
            )
        ).scalar_one_or_none()

        current = _metrics_from_row(row) if row is not None else Metrics()
        if seq <= current.last_seq:
            return False  # already folded (idempotent replay)

        new = apply_event(current, topic, payload, seq)
        if row is None:
            # id defaults to uuid7 (UUIDPrimaryKey mixin); created_at defaults to now().
            session.add(
                ConversationMetric(
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                    **{name: getattr(new, name) for name in _METRIC_FIELDS},
                )
            )
        else:
            for name in _METRIC_FIELDS:
                setattr(row, name, getattr(new, name))
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
    """Read one batch and fold conversation events into ``conversation_metrics``.

    Returns ``(entries_read, applied)``: ``entries_read`` is every stream entry consumed (incl.
    non-conversation topics and idempotent no-ops), ``applied`` is how many changed a row.
    ``from_id='>'`` reads new entries; ``'0'`` re-reads this consumer's pending (un-acked) entries
    for crash recovery. Every read entry is acked, so repeated ``'0'`` reads walk the PEL to empty.
    """
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


async def run_metrics(block_ms: int = 5000) -> None:
    """Consume ``relay:outbox`` forever, projecting metrics. Entry: ``relay reporting-metrics``.

    Single-instance: the fold is order-dependent (the ``seq <= last_seq`` guard is a hard drop, not
    a merge), so a second concurrent consumer could reorder a conversation's events across stream
    shards and silently drop an increment. A session-level advisory lock makes a second instance
    exit cleanly (mirrors the outbox relay).
    """
    redis = get_redis()
    await ensure_group(redis)
    async with get_engine().connect() as lock_conn:
        got = (
            await lock_conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": REPORTING_ADVISORY_LOCK}
            )
        ).scalar_one()
        if not got:
            log.info("reporting.metrics.already_running")
            return

        # Crash recovery: drain delivered-but-un-acked pending entries until the PEL is empty.
        # Terminate on entries READ, not rows changed — a full batch of already-applied no-ops still
        # advances the PEL and must not stop recovery early (else later un-applied entries strand).
        while (await consume_once(redis, from_id="0")).entries_read == BATCH_COUNT:
            pass
        log.info("reporting.metrics.started")
        while True:
            result = await consume_once(redis, from_id=">", block_ms=block_ms)
            if result.applied:
                log.info("reporting.metrics.applied", events=result.applied)


def main() -> None:
    asyncio.run(run_metrics())
