"""automation: workflows + versions + runs + run_steps (ledger) + timers

Revision ID: 0009_automation
Revises: 0008_webhooks
Create Date: 2026-07-24

P1.5 — RFC-001 §6.7 (workflow engine semantics), RFC-002 §5.6 (workflows/timers DDL).

Tenancy: all five tables are tenant tables — RLS enabled + FORCED via ``create_tenant_table``.
None are partitioned (RFC-002 §5.6; volumes are far below the parts/events firehoses), so none are
in ``scripts/check_migrations.LARGE_TABLES`` and plain ``op.create_index`` is used throughout.

Two SECURITY DEFINER helpers (owned by the BYPASSRLS ``migrator``, EXECUTE-granted to ``app_rw``,
mirroring ``relay_due_webhook_deliveries`` in 0008) let the workspace-agnostic beat scans see every
tenant's rows:
- ``relay_claim_due_timers`` — atomically CLAIMS due timers with **FOR UPDATE SKIP LOCKED** + a
  visibility lease (RFC-002 §5.6 W6); the beat task enqueues ``fire_timer`` for each.
- ``relay_due_workflow_runs`` — a pure SELECT of stuck ``running``/``suspended`` runs for the
  reaper (the advance/action task does the authoritative work under the run's row lock).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from relay.core.rls import create_tenant_table

revision: str = "0015_automation"
down_revision: str | None = "0014_integrations"
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


def _created_by_col() -> sa.Column:
    return sa.Column(
        "created_by", _UUID, sa.ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )


# Claim due timers across all workspaces (BYPASSRLS) with FOR UPDATE SKIP LOCKED + a lease. Returns
# the claimed rows so the beat task can enqueue ``fire_timer`` for each. The lease
# (``claimed_at < now() - lease``) reclaims a timer whose claiming worker crashed before firing.
_CLAIM_DUE_TIMERS = r"""
CREATE OR REPLACE FUNCTION relay_claim_due_timers(max_rows int, lease_seconds int)
RETURNS TABLE(workspace_id uuid, id uuid, run_id uuid, node_id text)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public AS $fn$
    UPDATE public.timers t
    SET claimed_by = 'beat', claimed_at = now()
    WHERE t.id IN (
        SELECT s.id FROM public.timers s
        WHERE s.status = 'pending'
          AND s.fire_at <= now()
          AND (s.claimed_by IS NULL OR s.claimed_at < now() - make_interval(secs => lease_seconds))
        ORDER BY s.fire_at
        FOR UPDATE SKIP LOCKED
        LIMIT max_rows
    )
    RETURNING t.workspace_id, t.id, t.run_id, t.node_id;
$fn$;
REVOKE ALL ON FUNCTION relay_claim_due_timers(int, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION relay_claim_due_timers(int, int) TO app_rw;
"""

# FIND stuck runs across all workspaces for the reaper (a pure SELECT — it does NOT mutate; the
# advance/action task claims via the run's row lock). ``running``/``suspended`` runs whose
# ``updated_at`` is older than ``stale_seconds`` lost their in-flight Celery message (e.g. a broker
# flush) and must be re-driven; ``waiting`` (timer-backed) and ``awaiting_input`` runs are parked
# legitimately and excluded.
_DUE_WORKFLOW_RUNS = r"""
CREATE OR REPLACE FUNCTION relay_due_workflow_runs(max_rows int, stale_seconds int)
RETURNS TABLE(workspace_id uuid, id uuid, status text, current_node_id text)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public AS $fn$
    SELECT r.workspace_id, r.id, r.status, r.current_node_id FROM public.workflow_runs r
    WHERE r.status IN ('running', 'suspended')
      AND r.updated_at < now() - make_interval(secs => stale_seconds)
    ORDER BY r.updated_at
    LIMIT max_rows;
$fn$;
REVOKE ALL ON FUNCTION relay_due_workflow_runs(int, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION relay_due_workflow_runs(int, int) TO app_rw;
"""


def upgrade() -> None:
    # --- workflows ---
    create_tenant_table(
        "workflows",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'inactive'")),
        sa.Column("active_version_id", _UUID, nullable=True),
        _created_by_col(),
        _updated_at_col(),
        sa.CheckConstraint("status IN ('inactive', 'active')", name="ck_workflows_status_valid"),
    )
    op.create_index(
        "ix_workflows_ws_active",
        "workflows",
        ["workspace_id"],
        postgresql_where=sa.text("status = 'active'"),
    )

    # --- workflow_versions ---
    create_tenant_table(
        "workflow_versions",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column(
            "workflow_id",
            _UUID,
            sa.ForeignKey("workflows.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("graph", pg.JSONB(), nullable=False),
        sa.Column("trigger_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'draft'")),
        _created_by_col(),
        sa.UniqueConstraint("workspace_id", "workflow_id", "version", name="uq_version_number"),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'archived')", name="ck_workflow_versions_status_valid"
        ),
    )
    op.create_index(
        "ix_workflow_versions_wf", "workflow_versions", ["workspace_id", "workflow_id", "version"]
    )

    # --- workflow_runs ---
    create_tenant_table(
        "workflow_runs",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column(
            "workflow_id",
            _UUID,
            sa.ForeignKey("workflows.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workflow_version_id", _UUID, nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'running'")),
        sa.Column("trigger_topic", sa.Text(), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("subject_kind", sa.Text(), nullable=True),
        sa.Column("subject_id", _UUID, nullable=True),
        sa.Column("context", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("current_node_id", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        _updated_at_col(),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("workspace_id", "workflow_id", "dedupe_key", name="uq_run_dedupe"),
        sa.CheckConstraint(
            "status IN ('running', 'waiting', 'suspended', 'awaiting_input', "
            "'completed', 'failed', 'cancelled')",
            name="ck_workflow_runs_status_valid",
        ),
    )
    # Execution-log listing (newest-first by id within a workflow).
    op.create_index("ix_workflow_runs_wf", "workflow_runs", ["workspace_id", "workflow_id", "id"])
    # The reaper's cross-workspace scan (status + age).
    op.create_index("ix_workflow_runs_reaper", "workflow_runs", ["status", "updated_at"])

    # --- workflow_run_steps (the exactly-once-effects ledger) ---
    create_tenant_table(
        "workflow_run_steps",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column(
            "run_id",
            _UUID,
            sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'started'")),
        sa.Column("action_type", sa.Text(), nullable=True),
        sa.Column("result", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("0")),
        _updated_at_col(),
        # THE exactly-once key (RFC-002 §5.6 "unique (run_id, step_id)").
        sa.UniqueConstraint("run_id", "node_id", name="uq_run_step"),
        sa.CheckConstraint(
            "status IN ('started', 'done', 'failed', 'skipped')",
            name="ck_workflow_run_steps_status_valid",
        ),
    )
    op.create_index(
        "ix_workflow_run_steps_log", "workflow_run_steps", ["workspace_id", "run_id", "id"]
    )

    # --- timers (durable waits, W6) ---
    create_tenant_table(
        "timers",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column(
            "run_id",
            _UUID,
            sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("fire_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'fired', 'cancelled')", name="ck_timers_status_valid"
        ),
    )
    # RFC-002 §5.6: partial index on (fire_at) for the unclaimed-pending due scan.
    op.create_index(
        "ix_timers_due",
        "timers",
        ["fire_at"],
        postgresql_where=sa.text("claimed_by IS NULL AND status = 'pending'"),
    )

    op.execute(_CLAIM_DUE_TIMERS)
    op.execute(_DUE_WORKFLOW_RUNS)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS relay_due_workflow_runs(int, int)")
    op.execute("DROP FUNCTION IF EXISTS relay_claim_due_timers(int, int)")
    op.drop_table("timers")
    op.drop_table("workflow_run_steps")
    op.drop_table("workflow_runs")
    op.drop_table("workflow_versions")
    op.drop_table("workflows")
