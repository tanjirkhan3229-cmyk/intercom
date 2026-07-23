"""Celery tasks for the `ai` module (P1.2) — Neko turns on the ai.interactive queue (RFC-001 §6.4).

A turn is slow (seconds), provider-bound and rate-limited, so it runs on its own bulkhead queue and
never on the interactive path. Idempotent: the pipeline's ``agent_runs`` claim gate makes a
redelivered trigger a no-op (master rule 3). Async pipeline code is reused verbatim via the
per-process asyncio bridge, so the turn shares one event loop + engine + router (breaker state) with
every other turn in the worker process.
"""

from __future__ import annotations

import uuid

from relay.core.asyncio_bridge import run_coro
from relay.core.logging import get_logger
from relay.modules.ai import service as ai_service
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
