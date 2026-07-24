"""SQLAlchemy models for the ``messaging`` module (RFC-002 §5.3 — the hottest domain).

Intercom-style model: a ``conversation`` is a head row and **everything that happens is an
append-only ``conversation_part``** (comment, note, assignment, state change, rating). Tickets
(later) are a 1:1 extension of conversations, not a parallel system.

Tables:
- ``conversations``      — head row; ``fillfactor=85`` (updated on every part → HOT-update
                           headroom, RFC-002 §5.3/§9); ``state`` is the closed-set enum
                           ``conversation_state`` (RFC-002 §5.1); ``snooze_shape`` CHECK.
- ``conversation_parts`` — append-only thread; ``bigint``-free UUIDv7 PK ordered within a
                           conversation; **monthly RANGE partitions**, PK ``(created_at, id)``;
                           ``body_tsv`` generated for FTS (R8); ``attachments``/``channel_meta``
                           JSONB (S3 refs / channel ids only — never bytes, RFC-001 A2).
- ``conversation_tags``  — tag names applied to a conversation (unique per conversation+name).
- ``saved_replies``      — canned agent responses (macros).

Postgres-specific bits (enum type, partial indexes, partition templates, generated column) are
authored in the migration (0003_messaging), which is the authoritative DDL. Never import this
module from another module — cross-module access is via ``service``/``events``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ENUM, JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from relay.core.base_model import Base, TimestampMixin, UUIDPrimaryKey, WorkspaceScoped

# --- Closed set: conversation state (Postgres enum, RFC-002 §5.1/§5.3) --------

CONVERSATION_STATES: tuple[str, ...] = ("open", "snoozed", "closed")
CONVERSATION_STATE_ENUM = ENUM(*CONVERSATION_STATES, name="conversation_state", create_type=False)

# --- Evolving-ish sets: text (validated in the service / by the DTO) ----------

# Author of a part (RFC-002 §5.3). ``system`` covers assignment/state_change bookkeeping.
AUTHOR_KINDS: tuple[str, ...] = ("contact", "admin", "ai_agent", "system")
# Part types delivered this phase (P0.3). More (bot, event, …) land later.
PART_TYPES: tuple[str, ...] = ("comment", "note", "assignment", "state_change", "rating")

# Channels the conversation can arrive on (RFC-002 §5.3). Only chat/email/api are wired in P0.
CHANNELS: tuple[str, ...] = (
    "chat",
    "email",
    "whatsapp",
    "messenger_fb",
    "instagram",
    "sms",
    "voice",
    "api",
)


class Conversation(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """The conversation head. One row per thread; updated on every part (W1)."""

    __tablename__ = "conversations"
    # ``fillfactor=85`` (HOT-update headroom — the head is updated on every part) is applied by
    # the migration via ALTER TABLE (SQLAlchemy has no Table-level storage-param kwarg).
    __table_args__ = (
        # RFC-002 §5.3: a snoozed conversation must carry a wake time. Enforced at the DB layer
        # (the service enforces valid *transitions*; this guards the state's *shape*).
        CheckConstraint("state <> 'snoozed' OR snoozed_until IS NOT NULL", name="snooze_shape"),
    )

    contact_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'chat'"))
    # FK to channel_accounts is added in P0.7 (that table lands with the channels module).
    channel_account_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    state: Mapped[str] = mapped_column(
        CONVERSATION_STATE_ENUM, nullable=False, server_default=sa.text("'open'")
    )
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("teams.id", ondelete="SET NULL"), nullable=True
    )
    priority: Mapped[bool] = mapped_column(nullable=False, server_default=sa.text("false"))
    # Set when awaiting an agent (contact spoke last); cleared when an agent replies. Drives R1.
    waiting_since: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    snoozed_until: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    last_part_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    first_contact_reply_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    ai_status: Mapped[str | None] = mapped_column(Text, nullable=True)


class ConversationPart(Base):
    """An append-only event in a conversation thread (RFC-002 §5.3).

    Monthly RANGE partitions; PK ``(created_at, id)`` (partition-key-leading). No PK ``default``
    here — ``id``/``created_at`` are set explicitly by the service so the head update and the
    part share one clock. RLS is enabled + forced on the partitioned parent by the migration.
    """

    __tablename__ = "conversation_parts"
    __table_args__ = (
        sa.PrimaryKeyConstraint("created_at", "id", name="pk_conversation_parts"),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    author_kind: Mapped[str] = mapped_column(Text, nullable=False)
    author_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    part_type: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        sa.Computed("to_tsvector('simple', coalesce(body, ''))", persisted=True),
        nullable=True,
    )
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
    )
    channel_meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    # Bookkeeping so state_change/assignment/rating parts render without a second table.
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    created_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


# --- Mobile push (P1.10) ------------------------------------------------------

# Platforms an SDK registers from. Kept a closed set so a typo can't create an unroutable token.
DEVICE_PLATFORMS: tuple[str, ...] = ("ios", "android")
# APNs distinguishes the sandbox (debug builds) and production push hosts; FCM ignores this.
DEVICE_ENVIRONMENTS: tuple[str, ...] = ("production", "sandbox")
# ``stale`` = the provider reported the token dead (APNs 410 / FCM NotRegistered); skip on fan-out.
DEVICE_STATUSES: tuple[str, ...] = ("active", "stale")

_PLATFORM_CHECK = "platform IN ('ios', 'android')"
_ENV_CHECK = "environment IN ('production', 'sandbox')"
_STATUS_CHECK = "status IN ('active', 'stale')"


class DeviceToken(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """An APNs/FCM token an iOS/Android SDK registered for a contact (P1.10, RFC-000 §2.1).

    Registration upserts on ``(workspace_id, token)`` so a rotated token just re-registers; the
    push fan-out flips ``status`` to ``stale`` when the provider rejects it.
    """

    __tablename__ = "device_tokens"
    __table_args__ = (
        CheckConstraint(_PLATFORM_CHECK, name="device_token_platform_valid"),
        CheckConstraint(_ENV_CHECK, name="device_token_environment_valid"),
        CheckConstraint(_STATUS_CHECK, name="device_token_status_valid"),
        UniqueConstraint("workspace_id", "token", name="uq_device_tokens_token"),
    )

    contact_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    token: Mapped[str] = mapped_column(Text, nullable=False)
    # APNs bundle id / Android package name — picks the APNs topic; null → the configured default.
    app_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    environment: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sa.text("'production'")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'active'"))
    last_seen_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class PushReceipt(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """Per-(message, device) dedupe ledger: the exactly-once gate for at-least-once push fan-out
    (master rule 3). ``message_id`` is a plain uuid (conversation_parts is partitioned, so its
    ``id`` can't be an FK target)."""

    __tablename__ = "push_receipts"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "message_id", "device_token_id", name="uq_push_receipts_dedupe"
        ),
    )

    message_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    device_token_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("device_tokens.id", ondelete="CASCADE"), nullable=False
    )
    provider_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)


class ConversationTag(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """A tag applied to a conversation (name-based; unique per conversation)."""

    __tablename__ = "conversation_tags"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "conversation_id", "name", name="uq_conversation_tags_conv_name"
        ),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)


class SavedReply(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """A canned agent response (macro). ``shortcut`` is the ``/`` trigger used in the composer."""

    __tablename__ = "saved_replies"
    __table_args__ = (
        UniqueConstraint("workspace_id", "shortcut", name="uq_saved_replies_shortcut"),
    )

    shortcut: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
