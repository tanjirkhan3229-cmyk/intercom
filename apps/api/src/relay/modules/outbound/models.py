"""SQLAlchemy models for the ``outbound`` module (P1.8 — RFC-002 §5.6, RFC-001 §6.7).

Two proactive surfaces plus the compliance spine that governs both.

Email broadcasts:
- ``campaigns`` — a broadcast definition (name, status, audience predicate, pinned version).
- ``campaign_versions`` — immutable MJML/subject/variables snapshots; a fire pins one version so
  editing after fire never mutates an in-flight send (mirrors workflow_versions).
- ``sends`` — the per-(campaign, contact) exactly-once ledger. **NON-partitioned** with a hard
  ``UNIQUE(workspace_id, campaign_id, contact_id)`` — the claim slot that makes concurrent workers
  and re-fires zero-duplicate (P1.8 acceptance #1). A deliberate deviation from RFC-002 §5.6
  ("sends ... monthly partitions"): a partitioned UNIQUE must include the partition key, which
  would let a later-month re-fire duplicate. Correctness beats the scale optimization; it is in
  ``check_migrations.LARGE_TABLES`` so its secondary indexes are built ``CONCURRENTLY``. (RFC-002
  updated in the same change.)

In-app posts & chats:
- ``posts`` — an in-app broadcast. ``kind='post'`` is a feed item; ``kind='chat'`` creates an
  outbound-initiated conversation per recipient.
- ``post_receipts`` — per-(post, contact) exactly-once delivery ledger (the in-app analogue of
  ``sends``); ``conversation_id`` is set for chat deliveries.

Compliance:
- ``subscription_types`` — opt-in categories (marketing vs transactional).
- ``consents`` — current-state projection, one row per (contact, type); the fast send-time read.
- ``consent_events`` — append-only audit trail (GDPR/CAN-SPAM proof of when/how consent changed).

Analytics:
- ``message_events`` — the unified delivery/engagement firehose for email + in-app. **Monthly
  RANGE partitions**, PK ``(created_at, id)`` (mirrors ``webhook_deliveries``). The append-only
  source of truth reconciled into ``campaign_stats``.
- ``campaign_stats`` — per-campaign rollup projection with a ``last_seq`` idempotency watermark.

Global infrastructure (NO ``workspace_id`` / NO RLS, like ``outbox``):
- ``outbound_event_dedupe`` — SES/SNS ``MessageId`` idempotency gate, checked before tenancy.

Postgres-specific DDL (RLS, partitions, CONCURRENTLY indexes) is authored in
``migrations/versions/0010_outbound.py`` — the authoritative DDL. Never import this module from
another module (boundary rule); cross-module access is via ``service`` / ``events``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from relay.core.base_model import Base, TimestampMixin, UUIDPrimaryKey, WorkspaceScoped

# --- Closed-ish sets: text + CHECK (RFC-002 §5.1 convention) -----------------------------------
CAMPAIGN_STATUSES: tuple[str, ...] = (
    "draft",
    "scheduled",
    "firing",
    "sent",
    "paused",
    "cancelled",
    "failed",
)
VERSION_STATUSES: tuple[str, ...] = ("draft", "published", "archived")
SEND_STATUSES: tuple[str, ...] = ("queued", "sending", "sent", "skipped", "failed")
SKIP_REASONS: tuple[str, ...] = (
    "suppressed",
    "unsubscribed",
    "no_consent",
    "freq_capped",
    "no_email",
    "contact_deleted",
    "paused",
)
MESSAGE_EVENT_SOURCES: tuple[str, ...] = ("email", "post", "chat")
MESSAGE_EVENT_KINDS: tuple[str, ...] = (
    "sent",
    "delivered",
    "open",
    "click",
    "bounce",
    "complaint",
    "unsub",
    "seen",
    "failed",
    "suppressed",
)
SUBSCRIPTION_KINDS: tuple[str, ...] = ("marketing", "transactional")
CONSENT_STATES: tuple[str, ...] = ("subscribed", "unsubscribed")
CONSENT_SOURCES: tuple[str, ...] = (
    "import",
    "api",
    "admin",
    "list_unsubscribe",
    "unsubscribe_page",
    "double_opt_in",
    "bounce_complaint",
)
CONSENT_ACTOR_KINDS: tuple[str, ...] = ("contact", "admin", "system")
POST_KINDS: tuple[str, ...] = ("post", "chat")
POST_RECEIPT_STATES: tuple[str, ...] = (
    "pending",
    "delivered",
    "seen",
    "clicked",
    "suppressed_consent",
    "suppressed_hard",
    "skipped",
)


def _sql_in(column: str, values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({joined})"


# --- Email broadcasts --------------------------------------------------------------------------


class Campaign(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """An email broadcast definition. A fire pins ``fired_version_id`` and moves through
    ``draft → scheduled → firing → sent`` (with ``paused``/``cancelled``/``failed`` interrupts)."""

    __tablename__ = "campaigns"
    __table_args__ = (
        CheckConstraint(_sql_in("channel", ("email",)), name="channel_valid"),
        CheckConstraint(_sql_in("status", CAMPAIGN_STATUSES), name="status_valid"),
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'email'"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'draft'"))
    # The predicate AST (core/predicates grammar) that defines the audience.
    segment: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    subscription_type_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("subscription_types.id", ondelete="SET NULL"),
        nullable=True,
    )
    # active_version_id / fired_version_id carry no FK (circular with campaign_versions.campaign_id,
    # mirroring workflows.active_version_id); integrity is enforced in the service layer.
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    fired_version_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    scheduled_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    fired_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    # Set once the audience snapshot + chunk enqueue completes — the fire idempotency latch.
    snapshot_done_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class CampaignVersion(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """An immutable template snapshot. A fire pins exactly one; editing creates a new version."""

    __tablename__ = "campaign_versions"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "campaign_id",
            "version",
            name="uq_campaign_versions_workspace_id_campaign_id_version",
        ),
        CheckConstraint(_sql_in("status", VERSION_STATUSES), name="status_valid"),
    )

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    preheader: Mapped[str | None] = mapped_column(Text, nullable=True)
    mjml: Mapped[str] = mapped_column(Text, nullable=False)
    from_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    reply_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Default values for template variables; reserved ``graph`` for series (Phase 2).
    variables: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    graph: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'draft'"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )


class Send(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """The per-(campaign, contact) exactly-once send ledger. NON-partitioned (see module docstring):
    the hard ``UNIQUE(workspace_id, campaign_id, contact_id)`` is the concurrency claim slot."""

    __tablename__ = "sends"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "campaign_id",
            "contact_id",
            name="uq_sends_workspace_id_campaign_id_contact_id",
        ),
        CheckConstraint(_sql_in("status", SEND_STATUSES), name="status_valid"),
        CheckConstraint(
            f"skip_reason IS NULL OR {_sql_in('skip_reason', SKIP_REASONS)}",
            name="skip_reason_valid",
        ),
    )

    # No FK on campaign_id / campaign_version_id / contact_id: keep the hot claim path lock-light
    # and allow a contact to be deleted mid-fire (resolved to skip_reason='contact_deleted').
    campaign_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    campaign_version_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    contact_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'queued'"))
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Deterministic RFC-822 Message-ID (stable across retries so an MTA dedupes crash re-sends).
    message_id: Mapped[str] = mapped_column(Text, nullable=False)
    provider_id: Mapped[str | None] = mapped_column(Text, nullable=True)  # SES MessageId
    sent_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class CampaignStats(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """Per-campaign rollup projection (mirrors ``conversation_metrics``): folded from the outbox by
    the stats consumer with a ``last_seq`` watermark, reconciled from ``message_events``."""

    __tablename__ = "campaign_stats"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "campaign_id", name="uq_campaign_stats_workspace_id_campaign_id"
        ),
    )

    campaign_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    audience_size: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("0"))
    sent: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("0"))
    delivered: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("0"))
    opened: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("0"))
    clicked: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("0"))
    bounced: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("0"))
    complained: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("0"))
    unsubscribed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("0"))
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("0"))
    failed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("0"))
    last_seq: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, server_default=sa.text("0")
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


# --- Compliance --------------------------------------------------------------------------------


class SubscriptionType(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """An opt-in category. Marketing types are consent-gated at send; transactional types are not.

    Transactional types skip the consent gate entirely.
    """

    __tablename__ = "subscription_types"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_subscription_types_workspace_id_name"),
        CheckConstraint(_sql_in("kind", SUBSCRIPTION_KINDS), name="kind_valid"),
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'marketing'"))
    # When true the marketing default flips to opt-in (absent consent row = not subscribed).
    requires_opt_in: Mapped[bool] = mapped_column(nullable=False, server_default=sa.text("false"))
    archived_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class Consent(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """Current consent state for one (contact, subscription_type) — the fast send-time lookup."""

    __tablename__ = "consents"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "contact_id",
            "subscription_type_id",
            name="uq_consents_workspace_id_contact_id_subscription_type_id",
        ),
        CheckConstraint(_sql_in("state", CONSENT_STATES), name="state_valid"),
        CheckConstraint(_sql_in("source", CONSENT_SOURCES), name="source_valid"),
    )

    contact_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    subscription_type_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("subscription_types.id", ondelete="CASCADE"),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    # Ties the projection to its provenance in consent_events (no FK: avoids ordering coupling).
    last_event_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class ConsentEvent(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """Append-only consent audit trail. One row per change; never mutated."""

    __tablename__ = "consent_events"
    __table_args__ = (
        CheckConstraint(
            f"from_state IS NULL OR {_sql_in('from_state', CONSENT_STATES)}",
            name="from_state_valid",
        ),
        CheckConstraint(_sql_in("to_state", CONSENT_STATES), name="to_state_valid"),
        CheckConstraint(_sql_in("source", CONSENT_SOURCES), name="source_valid"),
        CheckConstraint(
            f"actor_kind IS NULL OR {_sql_in('actor_kind', CONSENT_ACTOR_KINDS)}",
            name="actor_kind_valid",
        ),
    )

    contact_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    subscription_type_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("subscription_types.id", ondelete="CASCADE"),
        nullable=False,
    )
    from_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_state: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    actor_kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )


# --- In-app posts & chats ----------------------------------------------------------------------


class Post(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """An in-app broadcast. ``kind='post'`` is a feed item; ``kind='chat'`` starts a conversation.

    A fire snapshots the audience into ``post_receipts`` then delivers per gate precedence.
    """

    __tablename__ = "posts"
    __table_args__ = (
        CheckConstraint(_sql_in("kind", POST_KINDS), name="kind_valid"),
        CheckConstraint(_sql_in("status", CAMPAIGN_STATUSES), name="status_valid"),
    )

    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'post'"))
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    body: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'draft'"))
    segment: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    subscription_type_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("subscription_types.id", ondelete="SET NULL"),
        nullable=True,
    )
    scheduled_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    fired_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    snapshot_done_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    audience_size: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("0"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class PostReceipt(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """Per-(post, contact) exactly-once delivery ledger. The ``UNIQUE(workspace_id, post_id,
    contact_id)`` is the claim slot; ``conversation_id`` is set when a chat delivery makes one."""

    __tablename__ = "post_receipts"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "post_id",
            "contact_id",
            name="uq_post_receipts_workspace_id_post_id_contact_id",
        ),
        CheckConstraint(_sql_in("state", POST_RECEIPT_STATES), name="state_valid"),
    )

    post_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("posts.id", ondelete="CASCADE"), nullable=False
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'pending'"))
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    seen_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    clicked_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


# --- Analytics firehose (partitioned) ----------------------------------------------------------


class MessageEvent(Base):
    """Unified delivery/engagement event for email + in-app (RFC-002 §5.6). Monthly RANGE
    partitions; PK ``(created_at, id)`` (partition-key-leading). The append-only source of truth
    reconciled into ``campaign_stats``. RLS is enabled + forced on the partitioned parent.

    ``source_kind`` discriminates: ``email`` (source_id = campaign_id), ``post``/``chat``
    (source_id = post_id). ``campaign_id`` is denormalised for the email rollup grouping. No FKs
    (partitioned child).
    """

    __tablename__ = "message_events"
    __table_args__ = (
        sa.PrimaryKeyConstraint("created_at", "id", name="pk_message_events"),
        # Best-effort same-instant dedupe (NULL provider ids are distinct — in-app events dedupe via
        # post_receipts; email SES events dedupe via outbound_event_dedupe). Partition key included
        # because a partitioned UNIQUE must contain it.
        sa.UniqueConstraint(
            "created_at",
            "workspace_id",
            "provider_id",
            "event",
            "provider_event_id",
            name="uq_message_events_dedupe",
        ),
        sa.Index("message_events_rollup", "workspace_id", "campaign_id", "event"),
        sa.Index("message_events_source", "workspace_id", "source_kind", "source_id"),
        CheckConstraint(_sql_in("source_kind", MESSAGE_EVENT_SOURCES), name="source_kind_valid"),
        CheckConstraint(_sql_in("event", MESSAGE_EVENT_KINDS), name="event_valid"),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    email: Mapped[str | None] = mapped_column(CITEXT, nullable=True)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    provider_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_event_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


# --- Global infrastructure (NO workspace_id / NO RLS — like ``outbox``) ------------------------


class OutboundEventDedupe(Base):
    """SES/SNS ``MessageId`` idempotency gate, checked *before* the workspace is resolved. Not
    tenant scoped (routing happens after), so it is global infra, no RLS (mirrors InboundDedupe)."""

    __tablename__ = "outbound_event_dedupe"

    sns_message_id: Mapped[str] = mapped_column(Text, primary_key=True)
    received_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
