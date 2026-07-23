"""SQLAlchemy models for the ``channels`` module (P0.7 email — RFC-002 §5.6, RFC-001 §6.6).

Tenant-owned tables (RLS enabled + FORCED via ``create_tenant_table`` in ``0005_channels``):
- ``verified_domains``      — per-workspace sending domains (DKIM/SPF/DMARC). A GLOBAL partial
                              unique index on ``(domain) WHERE status='verified'`` makes inbound
                              routing deterministic across tenants (a domain verifies once).
- ``channel_accounts``      — an inbound support address bound to a domain; conversations point
                              at it via ``channel_account_id``. ``address`` is globally unique.
- ``email_messages``        — inbound + outbound message ledger.
                              ``UNIQUE(workspace_id, message_id)`` is the RFC-822 dedupe/threading
                              key; ``UNIQUE(workspace_id, part_id)`` is the outbound exactly-once
                              backstop.
- ``suppressions``          — hard bounces / complaints / manual entries; sends to these addresses
                              are blocked at the service layer.
- ``email_delivery_events`` — delivery-lifecycle audit (sent/bounce/complaint/blocked/...).

Global infrastructure tables (NO ``workspace_id`` / NO RLS, like ``outbox``): workers read them
before tenancy is known.
- ``channels_inbound_dedupe``  — SNS ``MessageId`` primary idempotency gate (pre-tenancy).
- ``channels_ingest_failures`` — DLQ replay log for un-routable / malformed inbound mail.

Postgres-specific DDL (RLS, partial/global unique indexes, SECURITY DEFINER resolvers) is
authored in ``migrations/versions/0005_channels.py`` — the authoritative DDL. Never import this
module from another module (boundary rule); cross-module access is via ``service`` / ``events``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from relay.core.base_model import Base, TimestampMixin, UUIDPrimaryKey, WorkspaceScoped

# Closed-ish sets: text + CHECK (RFC-002 §5.1 convention).
DOMAIN_STATUSES: tuple[str, ...] = ("pending", "verified", "failed")
ACCOUNT_STATUSES: tuple[str, ...] = ("active", "paused", "disabled")
SUPPRESSION_REASONS: tuple[str, ...] = ("bounce", "complaint", "manual")
MESSAGE_DIRECTIONS: tuple[str, ...] = ("in", "out")
DELIVERY_EVENTS: tuple[str, ...] = (
    "sent",
    "delivered",
    "bounce",
    "complaint",
    "blocked",
    "failed",
)


class VerifiedDomain(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """A per-workspace sending domain with its DKIM/SPF/DMARC verification state."""

    __tablename__ = "verified_domains"
    __table_args__ = (
        UniqueConstraint("workspace_id", "domain", name="uq_verified_domains_workspace_id_domain"),
        CheckConstraint("status IN ('pending', 'verified', 'failed')", name="status_valid"),
    )

    domain: Mapped[str] = mapped_column(CITEXT, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'pending'"))
    dkim_tokens: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
    )
    spf_ok: Mapped[bool] = mapped_column(nullable=False, server_default=sa.text("false"))
    dmarc_ok: Mapped[bool] = mapped_column(nullable=False, server_default=sa.text("false"))
    dns_records: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
    )
    verification_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class ChannelAccount(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """An inbound email address for a workspace, bound to a verified domain."""

    __tablename__ = "channel_accounts"
    __table_args__ = (
        UniqueConstraint("address", name="uq_channel_accounts_address"),
        CheckConstraint("channel IN ('email')", name="channel_valid"),
        CheckConstraint("status IN ('active', 'paused', 'disabled')", name="status_valid"),
    )

    channel: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'email'"))
    address: Mapped[str] = mapped_column(CITEXT, nullable=False)
    domain_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("verified_domains.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'active'"))
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )


class EmailMessage(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """Inbound/outbound email ledger: RFC-822 dedupe + threading + outbound exactly-once gate."""

    __tablename__ = "email_messages"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "message_id", name="uq_email_messages_workspace_id_message_id"
        ),
        UniqueConstraint("workspace_id", "part_id", name="uq_email_messages_workspace_id_part_id"),
        CheckConstraint("direction IN ('in', 'out')", name="direction_valid"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    part_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    message_id: Mapped[str] = mapped_column(Text, nullable=False)
    in_reply_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    # RFC-822 "References" header (a list of message-ids). Named ``email_references`` to avoid the
    # SQL reserved word ``references``.
    email_references: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    s3_raw_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    from_addr: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_addr: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)


class Suppression(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """A blocked recipient address (hard bounce / complaint / manual). Sends are refused."""

    __tablename__ = "suppressions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "email", name="uq_suppressions_workspace_id_email"),
        CheckConstraint("reason IN ('bounce', 'complaint', 'manual')", name="reason_valid"),
    )

    email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )


class EmailDeliveryEvent(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """Delivery-lifecycle audit for an outbound send (sent/bounce/complaint/blocked/...).

    Non-partitioned for P0.7 (low volume: agent replies + bounce notices). RFC-002 §5.6's
    partitioned ``message_events`` (campaign-scale) is deferred to the outbound module (P1.8) —
    documented in RFC-002.
    """

    __tablename__ = "email_delivery_events"
    __table_args__ = (
        CheckConstraint(
            "event IN ('sent', 'delivered', 'bounce', 'complaint', 'blocked', 'failed')",
            name="event_valid",
        ),
    )

    part_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )


# --- Global infrastructure (NO workspace_id / NO RLS — like ``outbox``) --------------------


class InboundDedupe(Base):
    """SNS ``MessageId`` idempotency gate, checked *before* the workspace is known. Not tenant
    scoped (routing happens after this), so it is global infra with no RLS."""

    __tablename__ = "channels_inbound_dedupe"

    sns_message_id: Mapped[str] = mapped_column(Text, primary_key=True)
    received_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class IngestFailure(UUIDPrimaryKey, TimestampMixin, Base):
    """DLQ replay log for inbound mail that could not be parsed or routed. Global (a failure can
    occur before the workspace is resolved), so ops can read every tenant's failures."""

    __tablename__ = "channels_ingest_failures"

    workspace_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    sns_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    s3_bucket: Mapped[str | None] = mapped_column(Text, nullable=True)
    s3_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
