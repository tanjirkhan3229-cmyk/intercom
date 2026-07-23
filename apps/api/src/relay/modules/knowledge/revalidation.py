"""Help Center ISR revalidation consumer (P0.8; RFC-001 §6.5 — mirrors ``realtime_fanout``).

The API never calls the Help Center site inline. On publish/unpublish/edit/delete of a
**published** article the ``knowledge`` service writes a ``knowledge.article.*`` outbox row in
the same transaction as the write (master rule 2); the outbox relay drains it to the
``relay:outbox`` Redis stream; *this* consumer reads the stream and POSTs the affected paths to
the site's on-demand revalidation webhook (``/api/revalidate``). So the ISR cache refreshes
within seconds of a publish (acceptance #1), with time-based ISR as the backstop.

Delivery is **at-least-once, deduped to an exactly-once effect** (same design as the fanout
consumer): a Redis consumer group tracks position and redelivers un-acked entries after a
crash; the POST happens *before* the done-marker, so a crash between them redelivers (never
drops); a ``hc:revalidate:done:{outbox_id}`` marker collapses the common redelivery.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from redis.exceptions import ResponseError

from relay.core.logging import get_logger
from relay.core.outbox import OUTBOX_STREAM
from relay.core.redis import get_redis
from relay.settings import get_settings

from . import events

log = get_logger(__name__)

GROUP = "knowledge-revalidation"
CONSUMER = "revalidate-1"
_DEDUPE_PREFIX = "hc:revalidate:done:"
_DEDUPE_TTL_SECONDS = 3600

# Called with the list of public paths to revalidate. Injected so tests can capture without HTTP.
Revalidator = Callable[[list[str]], Awaitable[None]]


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


def _paths_from(fields: dict[str, str]) -> list[str]:
    payload = json.loads(fields.get("payload") or "{}")
    paths = payload.get("paths")
    return [p for p in paths if isinstance(p, str)] if isinstance(paths, list) else []


async def _handle_entry(
    redis: Any, revalidate: Revalidator, group: str, entry_id: str, fields: dict[str, str]
) -> bool:
    """Process one stream entry. Returns True iff a fresh revalidation was performed.

    Robustness rules (so the consumer can neither drop nor wedge the stream):
    - A non-knowledge topic is acked and skipped (other consumer groups own it).
    - An already-done entry (redelivery after a crash between publish and ack) is acked.
    - A **malformed/poison** entry (missing ``outbox_id``, non-JSON payload) is logged and
      acked — it must never block the stream or crash-loop the process.
    - A **retryable** ``revalidate`` failure (site down) leaves the entry un-acked and returns
      without raising, so the steady-state loop's pending-drain reprocesses it (at-least-once).
    """
    topic = fields.get("topic", "")
    if topic not in events.REVALIDATION_TOPICS:
        await redis.xack(OUTBOX_STREAM, group, entry_id)
        return False
    try:
        outbox_id = fields["outbox_id"]
        paths = _paths_from(fields)
    except (KeyError, ValueError) as exc:  # JSONDecodeError is a ValueError
        log.warning("help_center.revalidate.skip_malformed", entry=str(entry_id), error=str(exc))
        await redis.xack(OUTBOX_STREAM, group, entry_id)
        return False
    if await _already_done(redis, outbox_id):
        await redis.xack(OUTBOX_STREAM, group, entry_id)
        return False
    if paths:
        try:
            await revalidate(paths)
        except Exception as exc:
            # Any failure here is retryable (site down / network) — never crash the loop; leave
            # the entry un-acked so a later pending-drain retries it.
            log.warning("help_center.revalidate.retry", entry=str(entry_id), error=str(exc))
            return False
    await _mark_done(redis, outbox_id)
    await redis.xack(OUTBOX_STREAM, group, entry_id)
    return True


async def consume_once(
    redis: Any,
    revalidate: Revalidator,
    *,
    group: str = GROUP,
    consumer: str = CONSUMER,
    from_id: str = ">",
    count: int = 200,
    block_ms: int | None = None,
) -> int:
    """Read one batch; revalidate paths for fresh article-lifecycle events. Returns the number
    of *new* (not-yet-processed) events handled. ``from_id=">"`` reads new entries; ``"0"``
    re-reads this consumer's pending (un-acked) entries — used both for crash recovery and to
    retry entries whose POST failed. Never raises (per-entry errors are handled in-place)."""
    resp = await redis.xreadgroup(
        group, consumer, {OUTBOX_STREAM: from_id}, count=count, block=block_ms
    )
    if not resp:
        return 0

    handled = 0
    for _stream, entries in resp:
        for entry_id, fields in entries:
            if await _handle_entry(redis, revalidate, group, entry_id, fields):
                handled += 1
    return handled


async def run_revalidation(block_ms: int = 5000) -> None:
    """Consume ``relay:outbox`` forever and POST revalidations to the Help Center site.

    Entry point: ``relay help-center-revalidate`` (its own process, like the outbox relay /
    realtime fanout). If no revalidation URL is configured the consumer still drains the stream
    (so it never backs up) and relies on time-based ISR — it logs that it is a no-op.
    """
    settings = get_settings()
    redis = get_redis()
    await ensure_group(redis)

    async with httpx.AsyncClient(timeout=5.0) as client:

        async def _revalidate(paths: list[str]) -> None:
            url = settings.help_center_revalidate_url
            if not url:
                log.warning("help_center.revalidate.skipped_no_url", paths=paths)
                return
            resp = await client.post(
                url,
                json={"paths": paths},
                headers={"X-Relay-Revalidate-Secret": settings.help_center_revalidate_secret},
            )
            resp.raise_for_status()

        # Crash recovery: drain any pending (delivered-but-un-acked) entries first.
        while await consume_once(redis, _revalidate, from_id="0") > 0:
            pass
        log.info("help_center.revalidate.started", url=settings.help_center_revalidate_url)
        while True:
            n = await consume_once(redis, _revalidate, from_id=">", block_ms=block_ms)
            # Also retry anything still pending in our PEL from an earlier failed POST, so a
            # transient site outage self-heals in steady state (not only on process restart).
            n += await consume_once(redis, _revalidate, from_id="0")
            if n:
                log.info("help_center.revalidate.done", events=n)


def main() -> None:
    asyncio.run(run_revalidation())
