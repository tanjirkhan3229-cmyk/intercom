"""Workflow trigger consumer (P1.5; mirrors webhooks/consumer.py).

Consumes ``relay:outbox`` and, for each event whose topic maps to a workflow **trigger key**, finds
the owning workspace's active workflows on that trigger, evaluates each trigger-filter predicate
against the event payload, and creates one ``workflow_run`` per match — then enqueues
``automation.advance_run`` for each. It sets the RLS GUC from the payload's ``workspace_id`` before
touching tenant tables (the stream is not tenant-scoped).

Exactly-once run creation is the ``workflow_runs`` UNIQUE
``(workspace_id, workflow_id, dedupe_key)`` constraint (``dedupe_key = "<trigger_key>:<outbox_id>"``
— a redelivered event yields no second
run). A Redis ``wf:triggered:{outbox_id}`` marker (set only *after* enqueue) collapses the common
relay redelivery. Like the webhook dispatcher it **never runs the workflow itself** — the executor
runs on the worker tier, so a slow/looping workflow can't block this single-instance stream drainer.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from redis.exceptions import ResponseError
from sqlalchemy.exc import IntegrityError

from relay.core.db import session_scope
from relay.core.ids import IdPrefix, decode_public_id
from relay.core.logging import get_logger
from relay.core.outbox import OUTBOX_STREAM
from relay.core.predicates import evaluate
from relay.core.redis import get_redis
from relay.settings import get_settings
from relay.worker import celery_app

from . import events, service

log = get_logger(__name__)

GROUP = "automation-triggers"
CONSUMER = "triggers-1"
_DEDUPE_PREFIX = "wf:triggered:"
_DEDUPE_TTL_SECONDS = 3600


async def ensure_group(redis: Any, *, group: str = GROUP) -> None:
    try:
        await redis.xgroup_create(OUTBOX_STREAM, group, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def _enqueue_advance(workspace_id: uuid.UUID, run_id: uuid.UUID) -> None:
    celery_app.send_task(
        "automation.advance_run", args=[str(workspace_id), str(run_id)], queue="interactive"
    )


def _subject(payload: dict[str, Any]) -> tuple[str | None, uuid.UUID | None]:
    """Derive the run's subject from an event payload (conversation preferred, else contact)."""
    conv = payload.get("conversation_id")
    if isinstance(conv, str):
        return "conversation", decode_public_id(IdPrefix.CONVERSATION, conv)
    contact = payload.get("contact_id")
    if isinstance(contact, str):
        return "contact", decode_public_id(IdPrefix.CONTACT, contact)
    return None, None


async def _create_runs(
    workspace_id: uuid.UUID,
    trigger_key: str,
    trigger_topic: str,
    outbox_id: uuid.UUID,
    payload: dict[str, Any],
) -> list[uuid.UUID]:
    """Create a run per matching active workflow (exactly-once). Returns the new run ids. The
    subject is derived here; a malformed subject id surfaces as ``ValueError`` and is handled
    (ack+skip) by the caller, so a bad payload can never wedge the stream."""
    dedupe_key = f"{trigger_key}:{outbox_id}"
    subject_kind, subject_id = _subject(payload)
    created: list[uuid.UUID] = []
    async with session_scope(workspace_id) as session:  # sets app.ws GUC
        matches = await service.active_workflows_for_trigger(session, trigger_key)
        for m in matches:
            if m.trigger_filter is not None and not evaluate(m.trigger_filter, payload):
                continue
            run_id = await service.create_run_from_trigger(
                session,
                workspace_id=workspace_id,
                workflow_id=m.workflow_id,
                version_id=m.version_id,
                entry_node_id=m.entry_node_id,
                trigger_topic=trigger_topic,
                dedupe_key=dedupe_key,
                subject_kind=subject_kind,
                subject_id=subject_id,
                context=payload,
            )
            if run_id is not None:
                created.append(run_id)
    return created


async def _handle_entry(redis: Any, group: str, entry_id: str, fields: dict[str, str]) -> bool:
    """Process one stream entry. Returns True iff at least one run was created.

    Never raises on a non-trigger / malformed entry (ack + skip so it can't wedge the stream); a
    retryable failure (DB/broker down) leaves the entry un-acked for a later pending drain.
    """
    if not get_settings().workflows_enabled:
        await redis.xack(OUTBOX_STREAM, group, entry_id)
        return False
    topic = fields.get("topic", "")
    if topic not in events.SUBSCRIBED_OUTBOX_TOPICS:
        await redis.xack(OUTBOX_STREAM, group, entry_id)
        return False
    try:
        outbox_id = uuid.UUID(fields["outbox_id"])
        payload = json.loads(fields.get("payload") or "{}")
        trigger_key = events.trigger_key_for(topic, payload)
        if trigger_key is None:
            await redis.xack(OUTBOX_STREAM, group, entry_id)
            return False
        workspace_id = decode_public_id(IdPrefix.WORKSPACE, payload["workspace_id"])
    except (KeyError, ValueError) as exc:  # JSONDecodeError is a ValueError
        log.warning("automation.trigger.skip_malformed", entry=str(entry_id), error=str(exc))
        await redis.xack(OUTBOX_STREAM, group, entry_id)
        return False

    # Loop guard: a domain event emitted BY a workflow action (marked ``origin=workflow``) must not
    # re-trigger workflows, or an "on contact.updated → set contact attribute" (or "on
    # state_changed → close") workflow would cascade forever (each action re-emits its trigger with
    # a fresh outbox id, so run-dedupe can't stop it). Standard automation-engine loop protection.
    if payload.get("origin") == "workflow":
        await redis.xack(OUTBOX_STREAM, group, entry_id)
        return False

    if await redis.exists(f"{_DEDUPE_PREFIX}{outbox_id}"):
        await redis.xack(OUTBOX_STREAM, group, entry_id)
        return False

    try:
        run_ids = await _create_runs(workspace_id, trigger_key, topic, outbox_id, payload)
        for run_id in run_ids:  # after commit → the worker sees the run row
            _enqueue_advance(workspace_id, run_id)
    except IntegrityError as exc:  # a concurrent creator won the dedupe; safe to treat as done
        log.info("automation.trigger.dedupe_race", entry=str(entry_id), error=str(exc))
        run_ids = []
    except (
        ValueError
    ) as exc:  # a malformed subject id in the payload → skip (can't wedge the stream)
        log.warning("automation.trigger.skip_bad_subject", entry=str(entry_id), error=str(exc))
        await redis.xack(OUTBOX_STREAM, group, entry_id)
        return False
    except Exception as exc:  # DB/broker hiccup → retry (leave un-acked)
        log.warning("automation.trigger.retry", entry=str(entry_id), error=str(exc))
        return False

    await redis.set(f"{_DEDUPE_PREFIX}{outbox_id}", "1", ex=_DEDUPE_TTL_SECONDS)
    await redis.xack(OUTBOX_STREAM, group, entry_id)
    return bool(run_ids)


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


async def run_consumer(block_ms: int = 5000) -> None:
    """Consume ``relay:outbox`` forever and fan out workflow triggers. Entry point: ``relay
    automation-triggers`` (its own process, like the outbox relay / webhook dispatch)."""
    redis = get_redis()
    await ensure_group(redis)
    while await consume_once(redis, from_id="0") > 0:  # crash recovery: drain pending first
        pass
    log.info("automation.triggers.started")
    while True:
        n = await consume_once(redis, from_id=">", block_ms=block_ms)
        n += await consume_once(redis, from_id="0")  # retry anything left un-acked
        if n:
            log.info("automation.triggers.dispatched", runs=n)


def main() -> None:
    asyncio.run(run_consumer())
