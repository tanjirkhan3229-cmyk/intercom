"""SQLAlchemy models for the ``identity`` module (RFC-002 §5, RFC-001 §10).

Tenancy note: two tables are deliberately **global** (no per-workspace RLS): ``workspaces``
(the tenant registry itself) and ``admins`` (a teammate account can belong to more than one
workspace via ``memberships``). They are never listed cross-tenant — the app layer always
scopes ``workspaces`` by id and ``admins`` by the authenticated principal. Everything else
here is a proper tenant table created via ``create_tenant_table`` (RLS enabled + forced).
``refresh_tokens`` and ``api_keys`` embed the workspace in their secret's prefix so the
workspace is known before the RLS GUC is set, keeping them RLS-protected too.
"""

from __future__ import annotations

import datetime as dt
import uuid

import sqlalchemy as sa
from sqlalchemy import ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from relay.core.base_model import Base, TimestampMixin, UUIDPrimaryKey, WorkspaceScoped

# Membership roles (RFC-001 §10). Evolving set → text + CHECK, not a Postgres enum.
ROLES: tuple[str, ...] = ("owner", "admin", "agent", "restricted")
_ROLE_CHECK = "role IN ('owner', 'admin', 'agent', 'restricted')"


class Workspace(UUIDPrimaryKey, TimestampMixin, Base):
    """Tenant root. Global (not RLS-scoped) — the registry every other table points at."""

    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    settings: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )


class Admin(UUIDPrimaryKey, TimestampMixin, Base):
    """A teammate account. Global — may hold memberships in multiple workspaces."""

    __tablename__ = "admins"

    email: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # Null when the account is OIDC-only.
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Google OIDC subject; unique when present.
    google_sub: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=sa.text("true"))


class Membership(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """Admin ↔ workspace link carrying the role. Tenant table (RLS)."""

    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("workspace_id", "admin_id", name="uq_memberships_workspace_id_admin_id"),
        sa.CheckConstraint(_ROLE_CHECK, name="role_valid"),
    )

    admin_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("admins.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)


class Team(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    __tablename__ = "teams"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_teams_workspace_id_name"),)

    name: Mapped[str] = mapped_column(Text, nullable=False)


class TeamMembership(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    __tablename__ = "team_memberships"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "team_id",
            "membership_id",
            name="uq_team_memberships_team_membership",
        ),
    )

    team_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    membership_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("memberships.id", ondelete="CASCADE"), nullable=False
    )


class ApiKey(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """Public-API key. Only the SHA-256 hash is stored; the key embeds the workspace prefix."""

    __tablename__ = "api_keys"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    key_prefix: Mapped[str] = mapped_column(Text, nullable=False)  # shown in UI
    key_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=sa.text("'{}'::text[]")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )
    last_used_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class RefreshToken(UUIDPrimaryKey, WorkspaceScoped, Base):
    """Rotating refresh token. Tenant table (RLS); the token value embeds the workspace prefix.

    Rotation: each refresh revokes the presented token and issues a successor in the same
    ``family_id``. Presenting an already-revoked token (reuse) revokes the whole family.
    """

    __tablename__ = "refresh_tokens"

    admin_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("admins.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    family_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    issued_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    expires_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(Text, nullable=True)
