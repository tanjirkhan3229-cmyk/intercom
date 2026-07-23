"""The ``agent_runs`` ledger + ``ai_settings`` snapshot (RFC-003 §3, §5-6).

Every Neko turn writes exactly one ``agent_runs`` row — the substrate for analytics, billing,
debugging and evals (RFC-003 §3). The lifecycle is claim → finalize:

- :func:`claim` inserts a ``pending`` row keyed on ``(workspace_id, trigger_part_id)`` with
  ``ON CONFLICT DO NOTHING``. A redelivered trigger (at-least-once, master rule 3) conflicts and
  returns ``None`` → the turn no-ops, never double-answers. This is the exactly-once gate.
- :func:`finalize` updates the claimed row with the full, replayable record and flips it to
  ``complete``, in the SAME transaction that persists the answer/handoff part (so the ledger and the
  visible effect commit atomically).

A crash between claim and finalize leaves a ``pending`` row; it is not retried (the gate blocks it).
A beat sweep of stale pending rows is a documented follow-up (ponytail) — not a dead-end for the
customer, who can always ask for a human.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from relay.core.ids import uuid7
from relay.modules.ai.models import AgentRun, AiSettings
from relay.settings import Settings, get_settings


@dataclass(frozen=True)
class AiSettingsView:
    """A read snapshot of a workspace's Neko config (defaults when no row exists)."""

    enabled: bool
    channels: list[str]
    grounding_threshold: float
    max_clarifications: int
    source_kinds: list[str] | None
    persona: str | None
    answer_max_tokens: int


def _default_settings_view(settings: Settings) -> AiSettingsView:
    return AiSettingsView(
        enabled=False,  # opt-in: a workspace enables Neko explicitly (RFC-003 §6/§8 shadow-first)
        channels=["chat"],
        grounding_threshold=settings.ai_grounding_threshold_default,
        max_clarifications=1,
        source_kinds=None,
        persona=None,
        answer_max_tokens=settings.ai_answer_max_tokens,
    )


async def load_settings(
    session: AsyncSession, workspace_id: uuid.UUID, *, settings: Settings | None = None
) -> AiSettingsView:
    """Load a workspace's Neko settings (RLS-scoped), falling back to safe defaults (Neko off)."""
    settings = settings or get_settings()
    row = await session.scalar(select(AiSettings).where(AiSettings.workspace_id == workspace_id))
    if row is None:
        return _default_settings_view(settings)
    return AiSettingsView(
        enabled=row.enabled,
        channels=list(row.channels),
        grounding_threshold=row.grounding_threshold,
        max_clarifications=row.max_clarifications,
        source_kinds=list(row.source_kinds) if row.source_kinds is not None else None,
        persona=row.persona,
        answer_max_tokens=row.answer_max_tokens,
    )


async def claim(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    trigger_part_id: uuid.UUID,
    query: str,
) -> uuid.UUID | None:
    """Atomically claim the turn for ``trigger_part_id``. Returns the new run id, or ``None`` if
    another worker already claimed it (the exactly-once gate)."""
    stmt = (
        pg_insert(AgentRun)
        .values(
            id=uuid7(),
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            trigger_part_id=trigger_part_id,
            query=query,
            status="pending",
        )
        .on_conflict_do_nothing(index_elements=["workspace_id", "trigger_part_id"])
        .returning(AgentRun.id)
    )
    run_id: uuid.UUID | None = await session.scalar(stmt)
    return run_id


async def count_clarifications(session: AsyncSession, conversation_id: uuid.UUID) -> int:
    """How many prior turns in this conversation already asked a clarifying question (RFC-003 §5:
    "one clarifying question max; never loops")."""
    n = await session.scalar(
        select(sa.func.count())
        .select_from(AgentRun)
        .where(
            AgentRun.conversation_id == conversation_id,
            AgentRun.outcome == "clarify",
            AgentRun.status == "complete",
        )
    )
    return int(n or 0)


@dataclass
class LedgerRecord:
    """Everything :func:`finalize` writes onto the claimed row (RFC-003 §3 ledger contract)."""

    outcome: str
    handoff_reason: str | None = None
    language: str | None = None
    safety_class: str | None = None
    rewritten_query: str | None = None
    retrieved: list[dict[str, Any]] = field(default_factory=list)
    grounding_score: float | None = None
    prompt_hash: str | None = None
    provider: str | None = None
    models: dict[str, Any] = field(default_factory=dict)
    answer: str | None = None
    citations: list[str] = field(default_factory=list)
    verdict: dict[str, Any] = field(default_factory=dict)
    tokens: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    latency_ms: dict[str, Any] = field(default_factory=dict)
    trace: dict[str, Any] = field(default_factory=dict)
    part_id: uuid.UUID | None = None


async def finalize(session: AsyncSession, run_id: uuid.UUID, record: LedgerRecord) -> None:
    """Write the full record onto the row + flip to ``complete`` (same txn as the effect)."""
    await session.execute(
        sa.update(AgentRun)
        .where(AgentRun.id == run_id)
        .values(
            status="complete",
            outcome=record.outcome,
            handoff_reason=record.handoff_reason,
            language=record.language,
            safety_class=record.safety_class,
            rewritten_query=record.rewritten_query,
            retrieved=record.retrieved,
            grounding_score=record.grounding_score,
            prompt_hash=record.prompt_hash,
            provider=record.provider,
            models=record.models,
            answer=record.answer,
            citations=record.citations,
            verdict=record.verdict,
            tokens=record.tokens,
            cost_usd=record.cost_usd,
            latency_ms=record.latency_ms,
            trace=record.trace,
            part_id=record.part_id,
            updated_at=sa.func.now(),
        )
    )


async def get_run(session: AsyncSession, run_id: uuid.UUID) -> AgentRun | None:
    return await session.get(AgentRun, run_id)


async def list_runs(
    session: AsyncSession, conversation_id: uuid.UUID, *, limit: int = 50
) -> list[AgentRun]:
    rows = await session.scalars(
        select(AgentRun)
        .where(AgentRun.conversation_id == conversation_id)
        .order_by(AgentRun.id.desc())
        .limit(limit)
    )
    return list(rows.all())
