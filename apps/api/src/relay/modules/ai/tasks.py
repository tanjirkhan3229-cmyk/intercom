"""Celery tasks for the `ai` module (P1.2) — Neko turns on the ai.interactive queue (RFC-001 §6.4).

A turn is slow (seconds), provider-bound and rate-limited, so it runs on its own bulkhead queue and
never on the interactive path. Idempotent: the pipeline's ``agent_runs`` claim gate makes a
redelivered trigger a no-op (master rule 3). Async pipeline code is reused verbatim via the
per-process asyncio bridge, so the turn shares one event loop + engine + router (breaker state) with
every other turn in the worker process.
"""

from __future__ import annotations

import datetime as dt
import uuid

from relay.core.asyncio_bridge import run_coro
from relay.core.db import session_scope
from relay.core.logging import get_logger
from relay.modules.ai import metering
from relay.modules.ai import service as ai_service
from relay.modules.messaging import service as messaging_service
from relay.worker import celery_app

log = get_logger(__name__)


@celery_app.task(name="ai.run_turn", queue="ai.interactive")
def run_turn(
    workspace_id: str, conversation_id: str, trigger_part_id: str
) -> dict[str, str | None]:
    """Run one Neko turn for a customer message (RFC-003 §3). Safely re-runnable."""
    result = run_coro(
        ai_service.run_turn(
            workspace_id=uuid.UUID(workspace_id),
            conversation_id=uuid.UUID(conversation_id),
            trigger_part_id=uuid.UUID(trigger_part_id),
        )
    )
    log.info(
        "ai.turn.done",
        conversation_id=conversation_id,
        outcome=result.outcome,
        reason=result.reason,
        run_id=str(result.run_id) if result.run_id else None,
    )
    return {
        "outcome": result.outcome,
        "run_id": str(result.run_id) if result.run_id else None,
        "reason": result.reason,
    }


@celery_app.task(name="ai.scan_silence_resolutions", queue="housekeeping")
def scan_silence_resolutions() -> int:
    """Meter conversations Neko answered that the customer left silent for 72 h (RFC-003 §8).

    The silence half of the resolution definition (the confirm half fires inline on the customer's
    confirm). Idempotent + re-runnable: a conversation already closed/metered is skipped, and each
    resolve rides its own per-workspace txn (close + meter atomic — master rule 2). Returns the
    number of resolutions metered this sweep."""
    return run_coro(_scan_silence_resolutions())


async def _scan_silence_resolutions() -> int:
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(hours=metering.SILENCE_HOURS)
    async with session_scope() as session:  # SECURITY DEFINER scan — no per-workspace GUC needed
        due = await messaging_service.neko_silence_due(session, cutoff)
    if len(due) >= 5000:  # sweep saturated — the next tick clears the tail (no silent drop)
        log.warning("ai.silence_sweep.saturated", due=len(due))
    metered = 0
    for workspace_id, conversation_id in due:
        async with session_scope(workspace_id) as session:
            if await ai_service.resolve_by_silence(
                session, workspace_id=workspace_id, conversation_id=conversation_id
            ):
                metered += 1
    if metered:
        log.info("ai.silence_sweep.metered", resolutions=metered)
    return metered
