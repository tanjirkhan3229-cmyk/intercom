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
from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Integer, Text, UniqueConstraint
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


# --- P1.7 Inbox v2 ------------------------------------------------------------


class OfficeHoursSchedule(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """A business-hours schedule (P1.7). One workspace default (``team_id`` NULL) plus one optional
    override per team — ``UNIQUE (workspace_id, team_id) NULLS NOT DISTINCT`` (PG16) makes the
    default row's NULL collide with itself so an upsert is well-defined.

    ``weekly`` maps a weekday string ``"0".."6"`` (Mon=0) to a list of ``{open, close}`` ``HH:MM``
    intervals; ``holidays`` is a list of ISO dates. The service parses/validates this into a
    :class:`~relay.modules.messaging.business_hours.BusinessHours` (RFC-002 §5.6).
    """

    __tablename__ = "office_hours_schedules"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "team_id",
            name="uq_office_hours_ws_team",
            postgresql_nulls_not_distinct=True,
        ),
    )

    team_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=True
    )
    timezone: Mapped[str] = mapped_column(Text, nullable=False)
    weekly: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    holidays: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )


# --- P1.7 SLA policies + applied state + event log ----------------------------

# SLA target keys (a policy sets a seconds budget for each it enforces; NULL = not enforced).
SLA_TARGETS: tuple[str, ...] = ("first_response", "next_response", "resolution")
# Lifecycle rows written to ``sla_events`` for reporting (RFC-002 §5.6).
SLA_EVENT_KINDS: tuple[str, ...] = ("applied", "met", "breached")


class SlaPolicy(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """An SLA policy (P1.7). Targets are seconds budgets; ``business_hours`` measures against the
    conversation's office-hours schedule (S1) rather than wall-clock. ``apply_predicate`` (a
    predicates AST, or NULL) auto-applies the policy to matching new conversations; ``escalation``
    is a small JSON of breach actions (``set_priority`` / ``notify`` / ``reassign_team_id``)."""

    __tablename__ = "sla_policies"
    __table_args__ = (
        CheckConstraint(
            "first_response_seconds IS NOT NULL OR next_response_seconds IS NOT NULL "
            "OR resolution_seconds IS NOT NULL",
            name="ck_sla_policies_has_target",
        ),
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(nullable=False, server_default=sa.text("true"))
    first_response_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_response_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resolution_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    business_hours: Mapped[bool] = mapped_column(nullable=False, server_default=sa.text("false"))
    apply_predicate: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    escalation: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    # Precedence when several policies' predicates match a new conversation (lowest wins).
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("0"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )


class ConversationSla(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """The applied-SLA state for one conversation (at most one policy at a time).

    Each target carries ``*_due_at`` (armed), ``*_satisfied_at`` (met), ``*_breached_at`` (missed).
    ``next_breach_at`` is the min of armed-and-unmet-and-unbreached due times — the single column
    the breach sweep scans; ``claimed_by``/``claimed_at`` give the sweep a ``FOR UPDATE SKIP
    LOCKED`` lease (mirrors ``timers``). ``last_seq`` is the outbox watermark for idempotent folds.
    """

    __tablename__ = "conversation_sla"
    __table_args__ = (
        UniqueConstraint("workspace_id", "conversation_id", name="uq_conversation_sla_conv"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    policy_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("sla_policies.id", ondelete="CASCADE"), nullable=False
    )
    applied_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)

    first_response_due_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    first_response_satisfied_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    first_response_breached_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    next_response_due_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    next_response_satisfied_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    next_response_breached_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    resolution_due_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    resolution_satisfied_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    resolution_breached_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    next_breach_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    active: Mapped[bool] = mapped_column(nullable=False, server_default=sa.text("true"))
    last_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=sa.text("0"))
    claimed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )


class SlaEvent(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """Append-only SLA lifecycle log for reporting (applied / met / breached per target)."""

    __tablename__ = "sla_events"
    __table_args__ = (
        CheckConstraint(
            "target IN ('first_response', 'next_response', 'resolution')",
            name="ck_sla_events_target_valid",
        ),
        CheckConstraint("kind IN ('applied', 'met', 'breached')", name="ck_sla_events_kind_valid"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    policy_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )


# --- P1.7 Custom inbox views --------------------------------------------------


class InboxView(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """A saved inbox filter (P1.7). ``filter`` is a predicates AST compiled to a ``conversations``
    WHERE clause (``views.ConversationViewResolver``). ``team_id`` set ⇒ shared with that team;
    NULL ⇒ a personal/workspace view owned by ``created_by``."""

    __tablename__ = "inbox_views"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    filter: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )


# --- P1.7 Agent availability (balanced assignment) ----------------------------


class AgentAvailability(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """Per-agent availability for balanced assignment (P1.7). ``away`` excludes the agent from
    auto-assignment; ``max_open`` caps their concurrent open conversations (NULL = uncapped).
    One row per (workspace, admin)."""

    __tablename__ = "agent_availability"
    __table_args__ = (
        UniqueConstraint("workspace_id", "admin_id", name="uq_agent_availability_admin"),
    )

    admin_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("admins.id", ondelete="CASCADE"), nullable=False
    )
    away: Mapped[bool] = mapped_column(nullable=False, server_default=sa.text("false"))
    max_open: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )
