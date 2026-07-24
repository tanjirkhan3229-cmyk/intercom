"""SQLAlchemy models for the ``integrations`` module (P1.9).

- ``integration_accounts`` — one per connected third-party account (Slack today). Tenant table
  (RLS enabled + forced). Secrets (Slack bot token + signing secret) live in ``config`` as Fernet
  ciphertext (core/crypto), never plaintext. Inbound Slack callbacks resolve the workspace by
  ``config->>'team_id'`` via a SECURITY DEFINER function (the request is unauthenticated, so it runs
  with no ``app.ws``), which is why the migration adds a GLOBAL partial-unique index on the active
  Slack ``team_id`` — one Slack workspace maps to exactly one Relay workspace.
- ``slack_thread_map`` — the load-bearing link between a Relay conversation and the Slack thread we
  posted for it (``thread_ts``). Outbound posts thread under it; an inbound reply in that thread is
  resolved back to the conversation by ``(channel_id, thread_ts)``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from relay.core.base_model import Base, TimestampMixin, UUIDPrimaryKey, WorkspaceScoped

INTEGRATION_TYPES: tuple[str, ...] = ("slack",)
_TYPE_CHECK = "integration_type IN ('slack')"
_STATUS_CHECK = "status IN ('active', 'paused', 'disabled')"


class IntegrationAccount(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """A connected third-party account. ``config`` holds type-specific settings + ciphertext secrets
    (Slack: team_id, team_name, channel_id, channel_name, bot_token_ciphertext,
    signing_secret_ciphertext)."""

    __tablename__ = "integration_accounts"
    __table_args__ = (
        CheckConstraint(_TYPE_CHECK, name="integration_type_valid"),
        CheckConstraint(_STATUS_CHECK, name="integration_status_valid"),
    )

    integration_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'active'"))
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class SlackThreadMap(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """Relay conversation ↔ Slack thread. One thread per conversation; inbound replies resolve back
    to the conversation via ``(channel_id, thread_ts)``."""

    __tablename__ = "slack_thread_map"
    __table_args__ = (
        # One thread per (account, conversation) — a workspace may connect >1 Slack account.
        UniqueConstraint(
            "workspace_id",
            "integration_account_id",
            "conversation_id",
            name="uq_slack_thread_map_conversation",
        ),
        # Inbound lookup: a Slack reply carries (channel, thread_ts) → the conversation.
        UniqueConstraint(
            "workspace_id", "channel_id", "thread_ts", name="uq_slack_thread_map_thread"
        ),
    )

    integration_account_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("integration_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    thread_ts: Mapped[str] = mapped_column(Text, nullable=False)
