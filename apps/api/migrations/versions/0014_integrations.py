"""integrations: integration_accounts + slack_thread_map + team resolver

Revision ID: 0014_integrations
Revises: 0013_imports
Create Date: 2026-07-24

P1.9 — Slack + Zapier. Two tenant tables (RLS enabled + FORCED via ``create_tenant_table``) plus a
SECURITY DEFINER resolver used by the UNAUTHENTICATED Slack inbound endpoint (no ``app.ws`` to scope
by), owned by the BYPASSRLS ``migrator`` and EXECUTE-granted to ``app_rw`` (mirrors the channels
inbound resolvers).

The active-Slack ``team_id`` index is **global** (not workspace-scoped) + unique: one Slack
workspace maps to exactly one Relay workspace, so an inbound callback resolves a single tenant.
Neither table is in LARGE_TABLES, so plain ``op.create_index`` is fine.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from relay.core.rls import create_tenant_table

revision: str = "0014_integrations"
down_revision: str | None = "0013_imports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = pg.UUID(as_uuid=True)

_TYPE_CHECK = "integration_type IN ('slack')"
_STATUS_CHECK = "status IN ('active', 'paused', 'disabled')"

# Resolve the active Slack account for a Slack team (unauthenticated inbound → bypasses RLS).
_TEAM_RESOLVER = r"""
CREATE OR REPLACE FUNCTION relay_slack_account_by_team(p_team_id text)
RETURNS TABLE(workspace_id uuid, signing_secret_ciphertext text)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public AS $fn$
    SELECT workspace_id, config->>'signing_secret_ciphertext'
    FROM public.integration_accounts
    WHERE integration_type = 'slack' AND status = 'active'
      AND config->>'team_id' = p_team_id
    LIMIT 1
$fn$;
REVOKE ALL ON FUNCTION relay_slack_account_by_team(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION relay_slack_account_by_team(text) TO app_rw;
"""


def _id_col() -> sa.Column:
    return sa.Column("id", _UUID, primary_key=True)


def _created_at_col() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def _workspace_fk() -> sa.Column:
    return sa.Column(
        "workspace_id", _UUID, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )


def upgrade() -> None:
    create_tenant_table(
        "integration_accounts",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("integration_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("config", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_by", _UUID, sa.ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(_TYPE_CHECK, name="integration_type_valid"),
        sa.CheckConstraint(_STATUS_CHECK, name="integration_status_valid"),
    )
    op.create_index("ix_integration_accounts_ws_id", "integration_accounts", ["workspace_id", "id"])
    # GLOBAL unique on the active Slack team_id (one Slack workspace ↔ one Relay workspace).
    op.create_index(
        "uq_integration_accounts_slack_team",
        "integration_accounts",
        [sa.text("(config->>'team_id')")],
        unique=True,
        postgresql_where=sa.text("integration_type = 'slack' AND status = 'active'"),
    )

    create_tenant_table(
        "slack_thread_map",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column(
            "integration_account_id",
            _UUID,
            sa.ForeignKey("integration_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            _UUID,
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("thread_ts", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "workspace_id",
            "integration_account_id",
            "conversation_id",
            name="uq_slack_thread_map_conversation",
        ),
        sa.UniqueConstraint(
            "workspace_id", "channel_id", "thread_ts", name="uq_slack_thread_map_thread"
        ),
    )

    op.execute(_TEAM_RESOLVER)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS relay_slack_account_by_team(text)")
    op.drop_table("slack_thread_map")
    op.drop_table("integration_accounts")
