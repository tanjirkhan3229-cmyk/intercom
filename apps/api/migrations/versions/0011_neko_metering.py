"""Neko product surface + resolution metering (P1.3)

Revision ID: 0011_neko_metering
Revises: 0010_ai_orchestrator
Create Date: 2026-07-24

P1.3 — Neko's workspace-facing controls and the money loop (RFC-003 §8-9, RFC-000 §8). All
expand-only (master rule 4): additive columns with defaults + additive indexes + one SECURITY
DEFINER resolver; no rewrites, no drops.

- ``ai_settings`` gains the product-surface controls: ``tone`` (friendly/neutral/formal),
  ``always_handoff_intents`` + ``office_hours_behavior`` (handoff rules), and the monthly
  ``monthly_spend_cap_usd`` (RFC-003 §9 — past it Neko routes to humans).
- ``usage_records`` gains ``stripe_synced_at`` (the async metering watermark) + two workspace-led
  indexes: the month-to-date spend window (cap + summary) and the un-synced sweep (Stripe push).
- ``plans`` gains ``metered_stripe_price_id`` (the per-resolution metered price; checkout attaches
  it once a Stripe metered price is provisioned — the async usage itself reports via a Billing
  Meter ``event_name``, not a per-item quantity).
- ``messaging_neko_silence_due`` — a STABLE SECURITY DEFINER resolver returning the open,
  Neko-handling conversations idle past a cutoff, so the 72 h-silence beat sweep runs cross-tenant
  without a per-workspace GUC (mirrors ``billing_workspace_by_stripe_subscription`` /
  ``channels_resolve_*``). ``conversations`` is a LARGE_TABLE, so no new index is added here — the
  bounded periodic sweep tolerates a filtered scan; a partial index lands (CONCURRENTLY) if the
  scan cost ever demands it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0011_neko_metering"
down_revision: str | None = "0010_ai_orchestrator"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SILENCE_DUE_FN = """
CREATE OR REPLACE FUNCTION messaging_neko_silence_due(cutoff timestamptz)
RETURNS TABLE(workspace_id uuid, conversation_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT workspace_id, id
    FROM conversations
    WHERE state = 'open' AND ai_status = 'active' AND last_part_at < cutoff
$$;
"""


def upgrade() -> None:
    # --- ai_settings: the P1.3 product-surface controls -----------------------------------------
    op.add_column(
        "ai_settings",
        sa.Column("tone", sa.Text(), nullable=False, server_default=sa.text("'neutral'")),
    )
    op.add_column(
        "ai_settings",
        sa.Column(
            "always_handoff_intents",
            pg.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "ai_settings",
        sa.Column(
            "office_hours_behavior",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'answer'"),
        ),
    )
    op.add_column("ai_settings", sa.Column("monthly_spend_cap_usd", sa.Numeric(), nullable=True))

    # --- usage_records: async-metering watermark + read indexes ---------------------------------
    op.add_column(
        "usage_records",
        sa.Column("stripe_synced_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Month-to-date spend window (spend cap + usage summary): workspace-led (RLS) → meter → time.
    op.create_index(
        "ix_usage_records_meter_window",
        "usage_records",
        ["workspace_id", "meter", "occurred_at"],
    )
    # Un-synced sweep (Stripe push): only rows not yet reported, workspace-led.
    op.create_index(
        "ix_usage_records_unsynced",
        "usage_records",
        ["workspace_id", "meter", "occurred_at"],
        postgresql_where=sa.text("stripe_synced_at IS NULL"),
    )

    # --- plans: optional per-resolution metered price -------------------------------------------
    op.add_column("plans", sa.Column("metered_stripe_price_id", sa.Text(), nullable=True))

    # --- 72 h-silence resolver (SECURITY DEFINER, cross-tenant sweep) ---------------------------
    op.execute(_SILENCE_DUE_FN)
    op.execute("REVOKE ALL ON FUNCTION messaging_neko_silence_due(timestamptz) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION messaging_neko_silence_due(timestamptz) TO app_rw")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS messaging_neko_silence_due(timestamptz)")
    op.drop_column("plans", "metered_stripe_price_id")
    op.drop_index("ix_usage_records_unsynced", table_name="usage_records")
    op.drop_index("ix_usage_records_meter_window", table_name="usage_records")
    op.drop_column("usage_records", "stripe_synced_at")
    op.drop_column("ai_settings", "monthly_spend_cap_usd")
    op.drop_column("ai_settings", "office_hours_behavior")
    op.drop_column("ai_settings", "always_handoff_intents")
    op.drop_column("ai_settings", "tone")
