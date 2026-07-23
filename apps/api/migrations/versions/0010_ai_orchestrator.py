"""ai orchestrator (Neko): agent_runs ledger + ai_settings

Revision ID: 0010_ai_orchestrator
Revises: 0009_knowledge_hub
Create Date: 2026-07-24

P1.2 — the Neko turn pipeline's persistent state (RFC-003 §3, §5-6). Both tables are tenant
tables (RLS enabled + FORCED via ``create_tenant_table``, master rule 1).

- ``agent_runs`` — one row per Neko turn (the ledger, RFC-003 §3): retrieval set, prompt hash,
  models, token counts, cost, latency breakdown, verdict, outcome, and a full reproducible
  ``trace``. ``UNIQUE(workspace_id, trigger_part_id)`` is the exactly-once gate against the
  at-least-once turn trigger (master rule 3). It is NOT (yet) a LARGE_TABLE — a workspace emits
  a couple of turns per resolved conversation; if the envelope proves otherwise it graduates to
  monthly partitioning like ``events`` (documented follow-up, not premature).
- ``ai_settings`` — one row per workspace: the per-workspace Neko kill switch + tunable grounding
  gate + channel scope + persona hook (RFC-003 §5-6).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from relay.core.rls import create_tenant_table

revision: str = "0010_ai_orchestrator"
down_revision: str | None = "0009_knowledge_hub"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = pg.UUID(as_uuid=True)


def _id_col() -> sa.Column:
    return sa.Column("id", _UUID, primary_key=True)


def _created_at_col() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def _updated_at_col() -> sa.Column:
    return sa.Column(
        "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def _workspace_fk() -> sa.Column:
    return sa.Column(
        "workspace_id", _UUID, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )


def upgrade() -> None:
    # --- ai_settings (per-workspace Neko config + kill switch) ----------------------------------
    create_tenant_table(
        "ai_settings",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        _updated_at_col(),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "channels",
            pg.JSONB(),
            nullable=False,
            server_default=sa.text("""'["chat"]'::jsonb"""),
        ),
        sa.Column("grounding_threshold", sa.Float(), nullable=False, server_default=sa.text("0.1")),
        sa.Column(
            "max_clarifications", sa.SmallInteger(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column("source_kinds", pg.JSONB(), nullable=True),
        sa.Column("persona", sa.Text(), nullable=True),
        sa.Column("answer_max_tokens", sa.Integer(), nullable=False, server_default=sa.text("400")),
        sa.UniqueConstraint("workspace_id", name="uq_ai_settings_workspace_id"),
    )

    # --- agent_runs (the per-turn ledger — RFC-003 §3) ------------------------------------------
    create_tenant_table(
        "agent_runs",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        _updated_at_col(),
        sa.Column("conversation_id", _UUID, nullable=False),
        sa.Column("trigger_part_id", _UUID, nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("handoff_reason", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("safety_class", sa.Text(), nullable=True),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("rewritten_query", sa.Text(), nullable=True),
        sa.Column("retrieved", pg.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("grounding_score", sa.Float(), nullable=True),
        sa.Column("prompt_hash", sa.Text(), nullable=True),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("models", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("citations", pg.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("verdict", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("tokens", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("latency_ms", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("trace", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("part_id", _UUID, nullable=True),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN "
            "('answered', 'clarify', 'handoff', 'ineligible', 'error')",
            name="ck_agent_runs_outcome_valid",
        ),
        sa.UniqueConstraint("workspace_id", "trigger_part_id", name="uq_agent_runs_trigger"),
    )
    # Run-inspector / analytics read path (P1.4): newest turns for a conversation, workspace-led.
    op.create_index(
        "ix_agent_runs_ws_conversation", "agent_runs", ["workspace_id", "conversation_id"]
    )


def downgrade() -> None:
    op.drop_table("agent_runs")
    op.drop_table("ai_settings")
