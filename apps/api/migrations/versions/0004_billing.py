"""billing: plans, subscriptions, usage_records, stripe_webhook_events

Revision ID: 0004_billing
Revises: 0003_messaging
Create Date: 2026-07-23

RFC-002 §5.6 (billing tables) + RFC-000 §8 (pricing open question — seats now, meters-ready).

Tenancy:
- ``plans`` is a global catalog (no RLS) — not workspace data, seeded here with a starter
  set of plans mapped to Stripe test-mode price ids (placeholders; override per environment).
- ``subscriptions`` and ``usage_records`` are tenant tables — RLS enabled + FORCED via
  ``create_tenant_table``.
- ``stripe_webhook_events`` is global (no RLS): the idempotency ledger for inbound webhooks,
  keyed by Stripe's own event id, checked *before* a workspace is resolved from the event
  payload — same rationale as the outbox having no RLS (relay.core.outbox).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from relay.core.ids import uuid7
from relay.core.rls import create_tenant_table

revision: str = "0004_billing"
down_revision: str | None = "0003_messaging"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = pg.UUID(as_uuid=True)
_TENANT_TABLES = ("usage_records", "subscriptions")


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
    # --- Global plan catalog (no RLS) ---
    op.create_table(
        "plans",
        _id_col(),
        _created_at_col(),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("stripe_price_id", sa.Text(), nullable=False),
        sa.Column("seat_based", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("trial_days", sa.Integer(), nullable=False, server_default=sa.text("14")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.UniqueConstraint("code", name="uq_plans_code"),
    )

    # --- Global webhook idempotency ledger (no RLS) ---
    op.create_table(
        "stripe_webhook_events",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # --- Tenant tables (RLS enabled + forced by create_tenant_table) ---
    create_tenant_table(
        "subscriptions",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("plan_id", _UUID, sa.ForeignKey("plans.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("stripe_customer_id", sa.Text(), nullable=True),
        sa.Column("stripe_subscription_id", sa.Text(), nullable=True),
        sa.Column("stripe_subscription_item_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'trialing'")),
        sa.Column("banner_state", sa.Text(), nullable=False, server_default=sa.text("'none'")),
        sa.Column("seats", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("seats_stripe_synced", sa.Integer(), nullable=True),
        sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("workspace_id", name="uq_subscriptions_workspace_id"),
        sa.UniqueConstraint(
            "stripe_subscription_id", name="uq_subscriptions_stripe_subscription_id"
        ),
        sa.CheckConstraint(
            "status IN ('trialing', 'active', 'past_due', 'canceled', 'unpaid')",
            name="ck_subscriptions_status_valid",
        ),
        sa.CheckConstraint(
            "banner_state IN ('none', 'trial_ending', 'payment_failed', 'canceled')",
            name="ck_subscriptions_banner_state_valid",
        ),
    )
    op.create_index("ix_subscriptions_workspace_id", "subscriptions", ["workspace_id"])

    create_tenant_table(
        "usage_records",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("meter", sa.Text(), nullable=False),
        sa.Column("qty", sa.Numeric(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "workspace_id", "meter", "source_id", name="uq_usage_records_workspace_meter_source"
        ),
    )
    op.create_index("ix_usage_records_workspace_id", "usage_records", ["workspace_id"])

    # --- Seed the starter plan catalog. Stripe price ids are environment placeholders;
    # ops swaps them for the real test/live price ids per environment (never baked secrets,
    # just non-secret catalog ids — still environment-specific so seeded, not hardcoded app
    # logic).
    plans_table = sa.table(
        "plans",
        sa.column("id", _UUID),
        sa.column("code", sa.Text()),
        sa.column("name", sa.Text()),
        sa.column("stripe_price_id", sa.Text()),
        sa.column("seat_based", sa.Boolean()),
        sa.column("trial_days", sa.Integer()),
    )
    op.execute(
        plans_table.insert().values(
            [
                {
                    "id": uuid7(),
                    "code": "starter",
                    "name": "Starter",
                    "stripe_price_id": "price_starter_placeholder",
                    "seat_based": True,
                    "trial_days": 14,
                },
                {
                    "id": uuid7(),
                    "code": "team",
                    "name": "Team",
                    "stripe_price_id": "price_team_placeholder",
                    "seat_based": True,
                    "trial_days": 14,
                },
            ]
        )
    )

    # --- Pre-GUC workspace lookup for webhook processing (mirrors 0001_identity's
    # identity_admin_workspaces): some Stripe event types (invoices) carry the subscription
    # id but no workspace metadata, so the owning workspace must be resolved *before* the
    # RLS GUC can be set. SECURITY DEFINER, owned by the BYPASSRLS migrator.
    op.execute(
        """
        CREATE FUNCTION billing_workspace_by_stripe_subscription(stripe_sub_id text)
        RETURNS uuid
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT workspace_id FROM subscriptions WHERE stripe_subscription_id = stripe_sub_id
        $$;
        """
    )
    op.execute("REVOKE ALL ON FUNCTION billing_workspace_by_stripe_subscription(text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION billing_workspace_by_stripe_subscription(text) TO app_rw")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS billing_workspace_by_stripe_subscription(text)")
    for table in _TENANT_TABLES:
        op.drop_table(table)
    op.drop_table("stripe_webhook_events")
    op.drop_table("plans")
