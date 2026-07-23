"""SQLAlchemy models for the `billing` module (RFC-002 §5.6 — Billing v1, P0.10).

``plans`` and ``stripe_webhook_events`` are deliberately **global** (no RLS): plans are a
shared catalog (not workspace data), and the webhook event ledger must be readable before a
workspace is known (RFC-002 §5.6 mirrors the outbox's own no-RLS rationale — see
``relay.core.outbox``). ``subscriptions`` and ``usage_records`` are proper tenant tables,
created via ``create_tenant_table`` (RLS enabled + forced).

Tenant-owned tables MUST be created via the create_tenant_table() Alembic helper so
that RLS is enabled + forced automatically (RFC-002 §7). Never import this module
from another module — go through `service`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy import ForeignKey, Numeric, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from relay.core.base_model import Base, TimestampMixin, UUIDPrimaryKey, WorkspaceScoped

# Evolving-ish sets: text + CHECK (not a Postgres enum), consistent with identity's role column.
SUBSCRIPTION_STATUSES: tuple[str, ...] = (
    "trialing",
    "active",
    "past_due",
    "canceled",
    "unpaid",
)
BANNER_STATES: tuple[str, ...] = ("none", "trial_ending", "payment_failed", "canceled")


class Plan(UUIDPrimaryKey, TimestampMixin, Base):
    """Global plan catalog (RFC-000 §8: seats now, meters-ready). Not tenant-scoped."""

    __tablename__ = "plans"

    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    stripe_price_id: Mapped[str] = mapped_column(Text, nullable=False)
    seat_based: Mapped[bool] = mapped_column(nullable=False, server_default=sa.text("true"))
    trial_days: Mapped[int] = mapped_column(nullable=False, server_default=sa.text("14"))
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=sa.text("true"))


class Subscription(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """One subscription per workspace (tenant table, RLS). The billing state of record.

    ``seats`` is our count (active memberships); ``seats_stripe_synced`` is the quantity we
    last successfully pushed to Stripe — the two diverge between a membership change and the
    next sync pass, which is exactly the "dirty" signal ``tasks.sync_seats_to_stripe`` polls
    for (RFC-001 §5: no Stripe call inside a request-path transaction).
    """

    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint("workspace_id", name="uq_subscriptions_workspace_id"),
        UniqueConstraint("stripe_subscription_id", name="uq_subscriptions_stripe_subscription_id"),
        sa.CheckConstraint(
            "status IN ('trialing', 'active', 'past_due', 'canceled', 'unpaid')",
            name="ck_subscriptions_status_valid",
        ),
        sa.CheckConstraint(
            "banner_state IN ('none', 'trial_ending', 'payment_failed', 'canceled')",
            name="ck_subscriptions_banner_state_valid",
        ),
    )

    plan_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("plans.id", ondelete="RESTRICT"), nullable=False
    )
    stripe_customer_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    stripe_subscription_item_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'trialing'"))
    banner_state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sa.text("'none'")
    )
    seats: Mapped[int] = mapped_column(nullable=False, server_default=sa.text("1"))
    # Last quantity successfully pushed to Stripe. NULL means "never synced yet".
    seats_stripe_synced: Mapped[int | None] = mapped_column(nullable=True)
    trial_ends_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    current_period_end: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    canceled_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class UsageRecord(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """Append-only usage meter (RFC-002 §5.6, W8). No update/delete — corrections are a new
    row with a negative ``qty`` against the same ``(meter, source_id)`` semantics. A generic
    interface: any module can call ``service.record_usage`` (e.g. Aide resolutions plug in at
    P1.3) without billing knowing what a "resolution" is.
    """

    __tablename__ = "usage_records"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "meter", "source_id", name="uq_usage_records_workspace_meter_source"
        ),
    )

    meter: Mapped[str] = mapped_column(Text, nullable=False)
    qty: Mapped[Any] = mapped_column(Numeric, nullable=False)
    occurred_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    # Natural key for idempotency (e.g. the triggering domain row's id). Unique per meter.
    source_id: Mapped[str] = mapped_column(Text, nullable=False)


class StripeWebhookEvent(Base):
    """Idempotency ledger for inbound Stripe webhooks (dedupe by Stripe's event id).

    Not tenant-scoped — the workspace is resolved from event metadata only *after* this
    dedupe check, so no RLS context exists yet at insert time (same rationale as the outbox).
    """

    __tablename__ = "stripe_webhook_events"

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # Stripe event id, e.g. "evt_..."
    type: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
