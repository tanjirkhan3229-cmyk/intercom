"""SQLAlchemy models for the ``webhooks`` module (P0.11, RFC-002 §5.6).

- ``webhook_subscriptions`` — one row per endpoint (url + encrypted signing secret + topics).
  Tenant table (RLS enabled + forced). ``secret_ciphertext`` is Fernet-encrypted, not hashed,
  because the delivery worker must recover the raw secret to compute the HMAC (see core/crypto).
- ``webhook_deliveries`` — append-only attempt log; monthly RANGE partitions, PK ``(created_at,
  id)`` (partition-key-leading), no FK to subscriptions (partitioned child — enforced in the app).
  In ``check_migrations.LARGE_TABLES``, so its indexes are inline partitioned templates (a plain
  ``CREATE INDEX`` on it would trip the migration linter).

Postgres-specific DDL (partitions, RLS, the retention drop function) lives in migration 0006.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from relay.core.base_model import Base, TimestampMixin, UUIDPrimaryKey, WorkspaceScoped


class WebhookSubscription(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """A customer endpoint subscribing to a set of topics. Tenant table (RLS)."""

    __tablename__ = "webhook_subscriptions"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled')", name="status_valid"),
        CheckConstraint("array_length(topics, 1) >= 1", name="topics_nonempty"),
    )

    url: Mapped[str] = mapped_column(Text, nullable=False)
    # Fernet token of the signing secret (recoverable to sign; a DB leak alone can't forge).
    secret_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    secret_last4: Mapped[str] = mapped_column(Text, nullable=False)  # for UI display
    topics: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'active'"))
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa.text("0")
    )
    disabled_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_success_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class WebhookDelivery(Base):
    """One attempted/scheduled delivery of one event to one subscription (RFC-002 §5.6).

    Monthly RANGE partitions; PK ``(created_at, id)``. ``id``/``created_at`` are set by the writer —
    ``created_at`` is the *dispatch instant* (so rows land in the current partition, retention is by
    dispatch age, and the 72h retry window runs from when delivery began).
    Unique ``(created_at, subscription_id, outbox_id)`` is a best-effort same-instant guard only;
    cross-dispatch dedupe is the Redis marker; delivery is **at-least-once** (receivers dedupe
    on the stable event id). RLS is enabled + forced on the partitioned parent by the migration.

    ``status`` lifecycle: pending → delivering → delivered | failed (→ retry) | exhausted
    (terminal) | skipped_breaker_open.
    """

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        sa.PrimaryKeyConstraint("created_at", "id", name="pk_webhook_deliveries"),
        sa.UniqueConstraint(
            "created_at", "subscription_id", "outbox_id", name="uq_webhook_deliveries_sub_outbox"
        ),
        sa.Index("webhook_deliveries_sub", "workspace_id", "subscription_id", "id"),
        sa.Index("webhook_deliveries_retry", "status", "next_attempt_at"),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    subscription_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    outbox_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("0"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'pending'"))
    response_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    delivered_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
