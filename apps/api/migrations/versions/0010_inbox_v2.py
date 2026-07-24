"""messaging inbox v2: office-hours + SLA + views + agent availability (P1.7)

Revision ID: 0010_inbox_v2
Revises: 0009_automation
Create Date: 2026-07-24

P1.7 — RFC-000 §2.2, RFC-002 §5.6. All tables are tenant tables — RLS enabled + FORCED via
``create_tenant_table``. None are partitioned (volumes are far below the parts/events firehoses),
so none are in ``scripts/check_migrations.LARGE_TABLES`` and plain ``op.create_index`` is used.

Grown across the P1.7 subsystems as they land (office-hours here; SLA / views / availability
follow in the same revision — nothing is shipped yet, so the revision stays a single unit).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from relay.core.rls import create_tenant_table

revision: str = "0010_inbox_v2"
down_revision: str | None = "0009_automation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = pg.UUID(as_uuid=True)

# Claim due SLA rows across all workspaces (BYPASSRLS, owned by ``migrator``) with FOR UPDATE SKIP
# LOCKED + a visibility lease — mirrors ``relay_claim_due_timers`` (0009). The beat sweep
# (``messaging.scan_sla_breaches``) calls this, then processes each claimed row under its workspace
# RLS. The lease reclaims a row whose claiming worker crashed before it recorded the breach.
_CLAIM_DUE_SLA = r"""
CREATE OR REPLACE FUNCTION relay_claim_due_sla(max_rows int, lease_seconds int)
RETURNS TABLE(workspace_id uuid, id uuid, conversation_id uuid)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public AS $fn$
    UPDATE public.conversation_sla t
    SET claimed_by = 'beat', claimed_at = now()
    WHERE t.id IN (
        SELECT s.id FROM public.conversation_sla s
        WHERE s.active
          AND s.next_breach_at IS NOT NULL
          AND s.next_breach_at <= now()
          AND (s.claimed_by IS NULL OR s.claimed_at < now() - make_interval(secs => lease_seconds))
        ORDER BY s.next_breach_at
        FOR UPDATE SKIP LOCKED
        LIMIT max_rows
    )
    RETURNING t.workspace_id, t.id, t.conversation_id;
$fn$;
REVOKE ALL ON FUNCTION relay_claim_due_sla(int, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION relay_claim_due_sla(int, int) TO app_rw;
"""


def _id_col() -> sa.Column:
    return sa.Column("id", _UUID, primary_key=True)


def _created_by_col() -> sa.Column:
    return sa.Column(
        "created_by", _UUID, sa.ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )


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
    # --- office_hours_schedules (S1) ---
    create_tenant_table(
        "office_hours_schedules",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("team_id", _UUID, sa.ForeignKey("teams.id", ondelete="CASCADE"), nullable=True),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("weekly", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("holidays", pg.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        _updated_at_col(),
        # PG16 NULLS NOT DISTINCT: the workspace default (team_id NULL) is unique against itself,
        # and each team has at most one override.
        sa.UniqueConstraint(
            "workspace_id",
            "team_id",
            name="uq_office_hours_ws_team",
            postgresql_nulls_not_distinct=True,
        ),
    )

    # --- sla_policies (S2) ---
    create_tenant_table(
        "sla_policies",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("first_response_seconds", sa.Integer(), nullable=True),
        sa.Column("next_response_seconds", sa.Integer(), nullable=True),
        sa.Column("resolution_seconds", sa.Integer(), nullable=True),
        sa.Column("business_hours", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("apply_predicate", pg.JSONB(), nullable=True),
        sa.Column("escalation", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("position", sa.Integer(), nullable=False, server_default=sa.text("0")),
        _created_by_col(),
        _updated_at_col(),
        sa.CheckConstraint(
            "first_response_seconds IS NOT NULL OR next_response_seconds IS NOT NULL "
            "OR resolution_seconds IS NOT NULL",
            name="ck_sla_policies_has_target",
        ),
    )
    # Auto-apply scan (active policies, precedence order).
    op.create_index(
        "ix_sla_policies_apply",
        "sla_policies",
        ["workspace_id", "position"],
        postgresql_where=sa.text("active AND apply_predicate IS NOT NULL"),
    )

    # --- conversation_sla (S2) — the applied state + breach clock ---
    create_tenant_table(
        "conversation_sla",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column(
            "conversation_id",
            _UUID,
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "policy_id", _UUID, sa.ForeignKey("sla_policies.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_response_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_response_satisfied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_response_breached_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_response_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_response_satisfied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_response_breached_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_satisfied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_breached_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_breach_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_seq", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        _updated_at_col(),
        sa.UniqueConstraint("workspace_id", "conversation_id", name="uq_conversation_sla_conv"),
    )
    # The breach sweep's due scan (RFC-002 §5.6 W6 timer idiom).
    op.create_index(
        "ix_conversation_sla_due",
        "conversation_sla",
        ["next_breach_at"],
        postgresql_where=sa.text("active AND next_breach_at IS NOT NULL"),
    )

    # --- sla_events (S2) — append-only reporting log ---
    create_tenant_table(
        "sla_events",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("conversation_id", _UUID, nullable=False),
        sa.Column("policy_id", _UUID, nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("meta", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint(
            "target IN ('first_response', 'next_response', 'resolution')",
            name="ck_sla_events_target_valid",
        ),
        sa.CheckConstraint(
            "kind IN ('applied', 'met', 'breached')", name="ck_sla_events_kind_valid"
        ),
    )
    op.create_index(
        "ix_sla_events_conv", "sla_events", ["workspace_id", "conversation_id", "created_at"]
    )
    op.create_index("ix_sla_events_report", "sla_events", ["workspace_id", "kind", "created_at"])

    op.execute(_CLAIM_DUE_SLA)

    # --- inbox_views (S3) — saved conversation filters ---
    create_tenant_table(
        "inbox_views",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("filter", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("team_id", _UUID, sa.ForeignKey("teams.id", ondelete="CASCADE"), nullable=True),
        _created_by_col(),
        _updated_at_col(),
    )
    # List views for a workspace (personal + team-shared), newest first is handled by the query.
    op.create_index("ix_inbox_views_ws_team", "inbox_views", ["workspace_id", "team_id"])

    # --- agent_availability (S4) — away toggle + concurrent-open cap ---
    create_tenant_table(
        "agent_availability",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column(
            "admin_id", _UUID, sa.ForeignKey("admins.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("away", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("max_open", sa.Integer(), nullable=True),
        _updated_at_col(),
        sa.UniqueConstraint("workspace_id", "admin_id", name="uq_agent_availability_admin"),
    )


def downgrade() -> None:
    op.drop_table("agent_availability")
    op.drop_table("inbox_views")
    op.execute("DROP FUNCTION IF EXISTS relay_claim_due_sla(int, int)")
    op.drop_table("sla_events")
    op.drop_table("conversation_sla")
    op.drop_table("sla_policies")
    op.drop_table("office_hours_schedules")
