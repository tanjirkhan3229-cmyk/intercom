"""The ``outbound-stats`` consumer: outbox stream → ``campaign_stats`` projection (P1.8).

A dedicated process (``relay outbound-stats``) reading ``relay:outbox`` via its own consumer group,
so it sees every campaign event once. A thin shell around the pure :mod:`reducer`: per event it
loads the campaign's stats row, folds the event in, and upserts. Idempotency is DB-durable via
``last_seq`` (mirrors ``reporting.consumer``): events for a campaign arrive in ``(aggregate_id,
seq)`` order, so at-least-once redelivery and full replay converge. Single-instance via a Postgres
advisory lock (the fold is order-dependent).
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

from . import events
from .models import CampaignStats
from .reducer import STATS_FIELDS, CampaignStatsAgg, apply_event

log = get_logger(__name__)

GROUP = "outbound-stats"
CONSUMER = "outbound-stats-1"
BATCH_COUNT = 200
# Distinct advisory-lock key from the reporting/relay consumers ("outstat").
OUTBOUND_STATS_LOCK = 0x006F_7574_7374_6174


class ConsumeResult(NamedTuple):
    entries_read: int
    applied: int


def _is_stats_topic(topic: str) -> bool:
    return any(topic.startswith(prefix) for prefix in events.CAMPAIGN_STATS_PREFIXES)


async def ensure_group(redis: Any, *, group: str = GROUP) -> None:
    try:
        await redis.xgroup_create(OUTBOX_STREAM, group, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def _agg_from_row(row: CampaignStats) -> CampaignStatsAgg:
    return CampaignStatsAgg(
        workspace_id=row.workspace_id,
        campaign_id=row.campaign_id,
        **{name: getattr(row, name) for name in STATS_FIELDS},
    )


async def _apply_to_db(topic: str, payload: dict[str, Any], seq: int) -> bool:
    ws_pub = payload.get("workspace_id")
    campaign_pub = payload.get("campaign_id")
    if not isinstance(ws_pub, str) or not isinstance(campaign_pub, str):
        return False
    workspace_id = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    campaign_id = decode_public_id(IdPrefix.CAMPAIGN, campaign_pub)

    async with session_scope(workspace_id) as session:
        row = (
            await session.execute(
                select(CampaignStats)
                .where(CampaignStats.campaign_id == campaign_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        current = (
            _agg_from_row(row)
            if row is not None
            else CampaignStatsAgg(workspace_id=workspace_id, campaign_id=campaign_id)
        )
        if seq <= current.last_seq:
            return False
        new = apply_event(current, topic, payload, seq)
        if row is None:
            session.add(
                CampaignStats(
                    workspace_id=workspace_id,
                    campaign_id=campaign_id,
                    **{name: getattr(new, name) for name in STATS_FIELDS},
                )
            )
        else:
            for name in STATS_FIELDS:
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
            # Isolate a malformed entry so it can't crash-loop this single-instance consumer.
            try:
                topic = fields.get("topic", "")
                if _is_stats_topic(topic):
                    payload = json.loads(fields.get("payload") or "{}")
                    seq = int(fields.get("seq") or 0)
                    if await _apply_to_db(topic, payload, seq):
                        applied += 1
            except Exception as exc:
                log.warning("outbound.stats.bad_entry", entry_id=str(entry_id), error=str(exc))
            await redis.xack(OUTBOX_STREAM, group, entry_id)
    return ConsumeResult(entries_read=entries_read, applied=applied)


async def run_stats(block_ms: int = 5000) -> None:
    """Consume ``relay:outbox`` forever, projecting stats. Entry: ``relay outbound-stats``."""
    redis = get_redis()
    await ensure_group(redis)
    async with get_engine().connect() as lock_conn:
        got = (
            await lock_conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": OUTBOUND_STATS_LOCK}
            )
        ).scalar_one()
        if not got:
            log.info("outbound.stats.already_running")
            return
        while (await consume_once(redis, from_id="0")).entries_read == BATCH_COUNT:
            pass
        log.info("outbound.stats.started")
        while True:
            result = await consume_once(redis, from_id=">", block_ms=block_ms)
            if result.applied:
                log.info("outbound.stats.applied", events=result.applied)


def main() -> None:
    asyncio.run(run_stats())
