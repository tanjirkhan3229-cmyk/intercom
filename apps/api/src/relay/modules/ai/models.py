"""SQLAlchemy models for the `ai` module (P1.2 — Neko orchestrator, RFC-003 §3).

Two tenant-owned tables (RLS enabled + FORCED via ``create_tenant_table``):

- ``agent_runs`` — the per-turn ledger. **Every** Neko turn writes one row, no exceptions
  (RFC-003 §3): retrieved chunk ids + scores, prompt hash, models, token counts, cost, latency
  breakdown, verdict, outcome, and a full reproducible ``trace`` (so any production answer replays
  from the row alone). ``UNIQUE (workspace_id, trigger_part_id)`` is the exactly-once gate — a
  redelivered trigger (at-least-once, master rule 3) claims the row once and never double-answers.
- ``ai_settings`` — one row per workspace: the per-workspace Neko kill switch (master rule / RFC-003
  §6) plus the tunable grounding gate, channel scope, and persona hook (P1.3 expands this).

Never import this module from another module — go through ``service``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Boolean, Float, Integer, SmallInteger, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from relay.core.base_model import Base, TimestampMixin, UUIDPrimaryKey, WorkspaceScoped

# Terminal outcomes of a turn (RFC-003 §3 state-machine leaves).
OUTCOMES: tuple[str, ...] = (
    "answered",  # grounded answer, verified, streamed + persisted
    "clarify",  # one clarifying question asked (grounding insufficient, first miss)
    "handoff",  # routed to a human (explicit ask, low grounding, safety, or verify reject)
    "ineligible",  # Neko disabled / out of scope for this workspace or channel
    "error",  # pipeline failed (providers exhausted) — degrades to human, never silence
)


class AgentRun(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """One Neko turn (RFC-003 §3). Reproducible from this row alone (replay tool)."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        # Exactly-once gate: at-least-once trigger delivery (master rule 3) can enqueue the same
        # customer part twice; the first turn claims the row, the redelivery conflicts and no-ops.
        sa.UniqueConstraint("workspace_id", "trigger_part_id", name="uq_agent_runs_trigger"),
        sa.Index("ix_agent_runs_ws_conversation", "workspace_id", "conversation_id"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    # The customer ``comment`` part that triggered this turn — the idempotency natural key.
    trigger_part_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)

    # ``pending`` while the turn runs (claimed, not yet committed); ``complete`` once the outcome +
    # ledger are written. A crash mid-turn leaves a pending row (swept later — ponytail in tasks).
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'pending'"))
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    handoff_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Preflight classification (cheap model).
    language: Mapped[str | None] = mapped_column(Text, nullable=True)
    safety_class: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Query understanding.
    query: Mapped[str] = mapped_column(Text, nullable=False)
    rewritten_query: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Retrieval set: [{chunk_id, source_id, source_kind, score}] — the evidence for the answer.
    retrieved: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
    )
    grounding_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Generation.
    prompt_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str | None] = mapped_column(Text, nullable=True)  # who served generation
    models: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    citations: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
    )

    # Verification (cheap model): {grounded, score, unsupported_claims, policy_flags}.
    verdict: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )

    # Accounting (RFC-003 §9). tokens: per-stage in/out; latency_ms: per-stage + first_token.
    tokens: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, server_default=sa.text("0"))
    latency_ms: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )

    # Full reproducible record: stage inputs/outputs + decisions. The replay tool re-runs the
    # deterministic pipeline from this and reproduces the answer + prompt hash exactly (RFC-003 §8).
    trace: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )

    # The conversation_part the answer/handoff-note was persisted as (null until committed).
    part_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)

    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class AiSettings(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """Per-workspace Neko configuration + kill switch (RFC-003 sec 5-6). One row per workspace."""

    __tablename__ = "ai_settings"
    __table_args__ = (sa.UniqueConstraint("workspace_id", name="uq_ai_settings_workspace_id"),)

    # Per-workspace Neko kill switch (RFC-003 §6). Off ⇒ every turn is ``ineligible`` (humans only).
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sa.text("false"))
    # Channels Neko may answer on (phase-1: chat only). Stored as a JSON array of channel names.
    channels: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'[\"chat\"]'::jsonb")
    )
    # Grounding-gate confidence floor (RFC-003 §5): below this ⇒ clarify once, then handoff. The
    # per-tenant conservative↔eager knob; the slider UI lands in P1.3.
    grounding_threshold: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=sa.text("0.1")
    )
    # Max clarifying questions before handoff (RFC-003 §5: "one clarifying question max").
    max_clarifications: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=sa.text("1")
    )
    # Source scope: which chunk source_kinds Neko may ground on (null ⇒ all). P1.3 surfaces this.
    source_kinds: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    # Persona/tone guidance folded into the system policy (P1.3 expands to friendly/neutral/formal).
    persona: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Answer-length budget hint (tokens) for generation (P1.3 surfaces the control).
    answer_max_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa.text("400")
    )

    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
