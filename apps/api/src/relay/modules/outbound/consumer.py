"""The ``outbound-fire`` consumer: dispatches campaign fires from the outbox (P1.8).

Consumes ``relay:outbox`` via its own group and, for each ``campaign.fired`` event, enqueues the
``outbound.fire_campaign`` worker task (which does the audience snapshot + chunked send enqueue).
Like the webhook/automation dispatchers it **never runs the snapshot itself** — a large snapshot
runs on the worker tier so it can't block this single-instance stream drainer. A Redis marker (set
after enqueue) collapses the common relay redelivery; true idempotency is the fire task's latch.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from redis.exceptions import ResponseError
from sqlalchemy import text

from relay.core.db import get_engine
from relay.core.ids import IdPrefix, decode_public_id
from relay.core.logging import get_logger
from relay.core.outbox import OUTBOX_STREAM
from relay.core.redis import get_redis
from relay.worker import celery_app

from . import events

log = get_logger(__name__)

GROUP = "outbound-fire"
CONSUMER = "outbound-fire-1"
BATCH_COUNT = 200
_DEDUPE_PREFIX = "outbound:fired:"
_DEDUPE_TTL_SECONDS = 3600
# Distinct advisory-lock key ("outfire").
OUTBOUND_FIRE_LOCK = 0x006F_7574_6669_7265


async def ensure_group(redis: Any, *, group: str = GROUP) -> None:
    try:
        await redis.xgroup_create(OUTBOX_STREAM, group, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def _dispatch(task_name: str, workspace_id: str, entity_id: str) -> None:
    celery_app.send_task(task_name, args=[workspace_id, entity_id], queue="housekeeping")


# Map a fire topic → (task name, payload id key).
_FIRE_DISPATCH: dict[str, tuple[str, str]] = {
    events.CAMPAIGN_FIRED: ("outbound.fire_campaign", "campaign_id"),
    events.POST_FIRED: ("outbound.fire_post", "post_id"),
}


async def consume_once(
    redis: Any,
    *,
    group: str = GROUP,
    consumer: str = CONSUMER,
    from_id: str = ">",
    count: int = BATCH_COUNT,
    block_ms: int | None = None,
) -> int:
    """Read one batch and dispatch a fire task per ``campaign.fired``. Returns entries read."""
    resp = await redis.xreadgroup(
        group, consumer, {OUTBOX_STREAM: from_id}, count=count, block=block_ms
    )
    if not resp:
        return 0
    entries_read = 0
    for _stream, entries in resp:
        for entry_id, fields in entries:
            entries_read += 1
            # A malformed entry must never wedge this single-instance consumer (a crash would
            # re-read the same poison row from the PEL on restart forever): log, skip, still ack.
            try:
                dispatch = _FIRE_DISPATCH.get(fields.get("topic", ""))
                if dispatch is not None:
                    task_name, id_key = dispatch
                    marker = f"{_DEDUPE_PREFIX}{entry_id}"
                    if not await redis.get(marker):
                        payload = json.loads(fields.get("payload") or "{}")
                        ws = payload.get("workspace_id")
                        entity = payload.get(id_key)
                        if isinstance(ws, str) and isinstance(entity, str):
                            prefix = IdPrefix.CAMPAIGN if id_key == "campaign_id" else IdPrefix.POST
                            _dispatch(
                                task_name,
                                str(decode_public_id(IdPrefix.WORKSPACE, ws)),
                                str(decode_public_id(prefix, entity)),
                            )
                            await redis.set(marker, "1", ex=_DEDUPE_TTL_SECONDS)
            except Exception as exc:
                log.warning("outbound.fire.bad_entry", entry_id=str(entry_id), error=str(exc))
            await redis.xack(OUTBOX_STREAM, group, entry_id)
    return entries_read


async def run_fire_dispatch(block_ms: int = 5000) -> None:
    """Consume ``relay:outbox`` forever, dispatching fires. Entry: ``relay outbound-fire``."""
    redis = get_redis()
    await ensure_group(redis)
    async with get_engine().connect() as lock_conn:
        got = (
            await lock_conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": OUTBOUND_FIRE_LOCK}
            )
        ).scalar_one()
        if not got:
            log.info("outbound.fire.already_running")
            return
        while await consume_once(redis, from_id="0") == BATCH_COUNT:
            pass
        log.info("outbound.fire.started")
        while True:
            await consume_once(redis, from_id=">", block_ms=block_ms)


def main() -> None:
    asyncio.run(run_fire_dispatch())
