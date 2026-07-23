"""The ``knowledge-indexing`` consumer (P1.1): outbox stream -> re-index Celery task.

A dedicated process reading ``relay:outbox`` via its **own** consumer group (independent of the
Help Center revalidation group), filtering article-lifecycle topics and enqueuing the
(re-)index / de-index task onto the ``ai.batch`` queue. It deliberately does NOT embed inline:
the single-instance stream drainer must stay fast, so the provider-bound work is bulkheaded onto
the worker fleet (mirrors the webhooks dispatch -> deliver split, RFC-001 §6.4).

Idempotency: a Redis ``done`` marker collapses the common redelivery; the enqueued task is itself
idempotent (content-hash diff), so a rare double-enqueue is harmless. Poison entries are logged +
acked, never left to wedge the stream. Entry point: ``relay knowledge-indexing``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from redis.exceptions import ResponseError

from relay.core.logging import get_logger
from relay.core.outbox import OUTBOX_STREAM
from relay.core.redis import get_redis
from relay.modules.knowledge import events

log = get_logger(__name__)

GROUP = "knowledge-indexing"
CONSUMER = "knowledge-indexing-1"
BATCH_COUNT = 200
_DEDUPE_PREFIX = "kb:index:done:"
_DEDUPE_TTL_SECONDS = 3600

# topic -> celery task name.
_REINDEX = "knowledge.reindex_article"
_DEINDEX = "knowledge.deindex_article"

Enqueuer = Callable[[str, str, str], None]


def _default_enqueue(task_name: str, workspace_id: str, article_id: str) -> None:
    from relay.worker import celery_app

    celery_app.send_task(task_name, args=[workspace_id, article_id], queue="ai.batch")


def _task_for(topic: str) -> str | None:
    if topic in (events.ARTICLE_PUBLISHED, events.ARTICLE_UPDATED):
        return _REINDEX
    if topic in (events.ARTICLE_UNPUBLISHED, events.ARTICLE_DELETED):
        return _DEINDEX
    return None


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
    enqueue: Enqueuer = _default_enqueue,
    *,
    group: str = GROUP,
    consumer: str = CONSUMER,
    from_id: str = ">",
    count: int = BATCH_COUNT,
    block_ms: int | None = None,
) -> int:
    """Read one batch, enqueue an index task per article event, ack. Returns entries read."""
    resp = await redis.xreadgroup(
        group, consumer, {OUTBOX_STREAM: from_id}, count=count, block=block_ms
    )
    if not resp:
        return 0
    entries_read = 0
    for _stream, entries in resp:
        for entry_id, fields in entries:
            entries_read += 1
            topic = fields.get("topic", "")
            task_name = _task_for(topic)
            if task_name is not None:
                outbox_id = fields.get("outbox_id", "")
                if not await _already_done(redis, outbox_id):
                    import json

                    payload = json.loads(fields.get("payload") or "{}")
                    workspace_id = payload.get("workspace_id")
                    article_id = fields.get("aggregate_id")
                    if workspace_id and article_id:
                        enqueue(task_name, workspace_id, article_id)
                        await _mark_done(redis, outbox_id)
                    else:
                        log.warning("knowledge.indexing.malformed", outbox_id=outbox_id)
            await redis.xack(OUTBOX_STREAM, group, entry_id)
    return entries_read


async def run_indexing(block_ms: int = 5000) -> None:
    redis = get_redis()
    await ensure_group(redis)
    while await consume_once(redis, from_id="0") == BATCH_COUNT:
        pass
    log.info("knowledge.indexing.started")
    while True:
        await consume_once(redis, from_id=">", block_ms=block_ms)
        await consume_once(redis, from_id="0")


def main() -> None:
    asyncio.run(run_indexing())
