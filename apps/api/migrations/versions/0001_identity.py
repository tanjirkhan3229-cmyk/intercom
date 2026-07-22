"""identity: workspaces, admins, memberships, teams, api_keys, refresh_tokens + RLS

Revision ID: 0001_identity
Revises:
Create Date: 2026-07-22

``workspaces`` and ``admins`` are global identity tables (no per-workspace RLS). Every other
table is created via ``create_tenant_table`` so RLS is enabled + FORCED with the canonical
``ws_isolation`` policy. Also installs ``identity_admin_workspaces`` — a SECURITY DEFINER
function used at login to find an admin's workspaces before the RLS GUC can be set.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from relay.core.rls import create_tenant_table

revision: str = "0001_identity"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = pg.UUID(as_uuid=True)
_TENANT_TABLES = ("refresh_tokens", "api_keys", "team_memberships", "teams", "memberships")


def _id_col() -> sa.Column:
    return sa.Column("id", _UUID, primary_key=True)


def _created_at_col() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def _workspace_fk() -> sa.Column:
    return sa.Column(
        "workspace_id",
        _UUID,
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )


def upgrade() -> None:
    # --- Global identity tables (no RLS) ---
    op.create_table(
        "workspaces",
        _id_col(),
        _created_at_col(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", pg.CITEXT(), nullable=False),
        sa.Column("settings", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.UniqueConstraint("slug", name="uq_workspaces_slug"),
    )

    op.create_table(
        "admins",
        _id_col(),
        _created_at_col(),
        sa.Column("email", pg.CITEXT(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("google_sub", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.UniqueConstraint("email", name="uq_admins_email"),
        sa.UniqueConstraint("google_sub", name="uq_admins_google_sub"),
    )

    # --- Tenant tables (RLS enabled + forced by create_tenant_table) ---
    create_tenant_table(
        "memberships",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column(
            "admin_id", _UUID, sa.ForeignKey("admins.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("role", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "workspace_id", "admin_id", name="uq_memberships_workspace_id_admin_id"
        ),
        sa.CheckConstraint(
            "role IN ('owner', 'admin', 'agent', 'restricted')", name="ck_memberships_role_valid"
        ),
    )
    op.create_index("ix_memberships_workspace_id", "memberships", ["workspace_id"])

    create_tenant_table(
        "teams",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.UniqueConstraint("workspace_id", "name", name="uq_teams_workspace_id_name"),
    )
    op.create_index("ix_teams_workspace_id", "teams", ["workspace_id"])

    create_tenant_table(
        "team_memberships",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("team_id", _UUID, sa.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "membership_id",
            _UUID,
            sa.ForeignKey("memberships.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "workspace_id", "team_id", "membership_id", name="uq_team_memberships_team_membership"
        ),
    )
    op.create_index("ix_team_memberships_workspace_id", "team_memberships", ["workspace_id"])

    create_tenant_table(
        "api_keys",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("key_prefix", sa.Text(), nullable=False),
        sa.Column("key_hash", sa.Text(), nullable=False),
        sa.Column(
            "scopes", pg.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")
        ),
        sa.Column(
            "created_by", _UUID, sa.ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
    )
    op.create_index("ix_api_keys_workspace_id", "api_keys", ["workspace_id"])

    create_tenant_table(
        "refresh_tokens",
        _id_col(),
        _workspace_fk(),
        sa.Column(
            "admin_id", _UUID, sa.ForeignKey("admins.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("family_id", _UUID, nullable=False),
        sa.Column(
            "issued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by", _UUID, nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("ip", sa.Text(), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )
    op.create_index("ix_refresh_tokens_workspace_id", "refresh_tokens", ["workspace_id"])

    # --- Login workspace discovery: SECURITY DEFINER (owned by BYPASSRLS migrator) ---
    op.execute(
        """
        CREATE FUNCTION identity_admin_workspaces(admin_id_param uuid)
        RETURNS TABLE(workspace_id uuid, role text)
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT m.workspace_id, m.role
            FROM memberships m
            WHERE m.admin_id = admin_id_param
        $$;
        """
    )
    op.execute("REVOKE ALL ON FUNCTION identity_admin_workspaces(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION identity_admin_workspaces(uuid) TO app_rw")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS identity_admin_workspaces(uuid)")
    for table in _TENANT_TABLES:
        op.drop_table(table)
    op.drop_table("admins")
    op.drop_table("workspaces")
