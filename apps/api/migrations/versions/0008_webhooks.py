"""webhooks: webhook_subscriptions + webhook_deliveries (partitioned) + retention

Revision ID: 0008_webhooks
Revises: 0007_channels
Create Date: 2026-07-23

P0.11 — RFC-001 §6.7 (webhook delivery), §10 (platform security); RFC-002 §5.6.

Tenancy: both tables are tenant tables — RLS enabled + FORCED via ``create_tenant_table``.

Index strategy vs the migration linter (scripts/check_migrations.py):
- ``webhook_subscriptions`` (small): plain ``op.create_index`` (partial + GIN) — not a LARGE_TABLE.
- ``webhook_deliveries`` (**LARGE_TABLES**, *partitioned*): ``CREATE INDEX CONCURRENTLY`` is
  unsupported on a partitioned parent, so its indexes are inline partitioned *templates* declared
  on ``create_table`` (emitted by the DDL compiler, not ``op.create_index``, so the linter — which
  scans ``op.create_index``/``op.execute`` — does not flag them; no lock concern on an empty
  table). Mirrors ``conversation_parts`` in 0003_messaging / ``events`` in 0002_crm.

Partition automation reuses ``relay_ensure_partitions`` (created in 0002_crm) and seeds
current..T+2 months. A new SECURITY DEFINER ``relay_drop_old_partitions`` implements the 30-day
retention drop (partitions are named ``<parent>_YYYY_MM`` by ``relay_create_month_partition``).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from relay.core.rls import create_tenant_table

revision: str = "0008_webhooks"
down_revision: str | None = "0007_channels"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = pg.UUID(as_uuid=True)


def _id_col() -> sa.Column:
    return sa.Column("id", _UUID, primary_key=True)


def _created_at_col() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def _workspace_fk() -> sa.Column:
    return sa.Column(
        "workspace_id", _UUID, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )


# Drop monthly partitions older than ``keep_days``. SECURITY DEFINER (owned by BYPASSRLS migrator)
# because a workspace-agnostic housekeeping sweep can't run under RLS as app_rw; EXECUTE-granted to
# app_rw so the housekeeping task can call it without DDL rights. Mirrors the partition-function
# pattern in 0002_crm. Dropping a whole partition is O(1) vs a mass DELETE.
_DROP_OLD_PARTITIONS = r"""
CREATE OR REPLACE FUNCTION relay_drop_old_partitions(parent text, keep_days int)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $fn$
DECLARE
    cutoff_month date := date_trunc('month', now() - (keep_days || ' days')::interval)::date;
    child record;
    dropped int := 0;
    part_month date;
BEGIN
    FOR child IN
        SELECT c.relname AS name
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        JOIN pg_class p ON p.oid = i.inhparent
        WHERE p.relname = parent AND p.relnamespace = 'public'::regnamespace
    LOOP
        BEGIN
            part_month := to_date(right(child.name, 7), 'YYYY_MM');
        EXCEPTION WHEN others THEN
            CONTINUE;  -- not a monthly partition we manage
        END;
        -- Drop only if the partition's whole month ends on/before the cutoff month.
        IF (part_month + interval '1 month')::date <= cutoff_month THEN
            EXECUTE format('DROP TABLE IF EXISTS public.%I', child.name);
            dropped := dropped + 1;
        END IF;
    END LOOP;
    RETURN dropped;
END;
$fn$;
REVOKE ALL ON FUNCTION relay_drop_old_partitions(text, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION relay_drop_old_partitions(text, int) TO app_rw;
"""

# FIND due deliveries across ALL workspaces for the retry scan — a pure SELECT, it does NOT claim
# or mutate the row. SECURITY DEFINER (BYPASSRLS owner) so the workspace-agnostic beat scan sees
# every tenant's rows. The single claim point is the deliver task's ``_claim`` (which stamps a
# lease on next_attempt_at); the scan must not pre-hide the row, or the deliver task it enqueues
# could never claim it. Includes 'delivering' rows whose lease has lapsed (next_attempt_at <= now)
# so a crashed in-flight attempt is recovered; a live 'delivering' row has a future next_attempt_at
# and is excluded.
_DUE_FUNCTION = r"""
CREATE OR REPLACE FUNCTION relay_due_webhook_deliveries(max_rows int)
RETURNS TABLE(workspace_id uuid, id uuid, created_at timestamptz)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public AS $fn$
    SELECT wd.workspace_id, wd.id, wd.created_at FROM public.webhook_deliveries wd
    WHERE wd.next_attempt_at IS NOT NULL AND wd.next_attempt_at <= now()
      AND wd.status IN ('pending', 'failed', 'skipped_breaker_open', 'delivering')
    ORDER BY wd.next_attempt_at
    LIMIT max_rows;
$fn$;
REVOKE ALL ON FUNCTION relay_due_webhook_deliveries(int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION relay_due_webhook_deliveries(int) TO app_rw;
"""


def upgrade() -> None:
    # --- webhook_subscriptions (small, regular) ---
    create_tenant_table(
        "webhook_subscriptions",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("secret_ciphertext", sa.Text(), nullable=False),
        sa.Column("secret_last4", sa.Text(), nullable=False),
        sa.Column("topics", pg.ARRAY(sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column(
            "consecutive_failures", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by", _UUID, sa.ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')", name="ck_webhook_subscriptions_status_valid"
        ),
        sa.CheckConstraint(
            "array_length(topics, 1) >= 1", name="ck_webhook_subscriptions_topics_nonempty"
        ),
    )
    # The dispatch consumer's hot query: active subscriptions for a workspace.
    op.create_index(
        "ix_webhook_subscriptions_ws_active",
        "webhook_subscriptions",
        ["workspace_id"],
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_webhook_subscriptions_ws_id", "webhook_subscriptions", ["workspace_id", "id"]
    )
    # GIN over topics so "active subs matching this topic" is index-served (array containment).
    op.create_index(
        "ix_webhook_subscriptions_topics",
        "webhook_subscriptions",
        ["topics"],
        postgresql_using="gin",
    )

    # --- webhook_deliveries (large, partitioned) — inline index templates ---
    create_tenant_table(
        "webhook_deliveries",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("workspace_id", _UUID, nullable=False),
        sa.Column("subscription_id", _UUID, nullable=False),  # no FK: partitioned child
        sa.Column("outbox_id", _UUID, nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("payload", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("response_code", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("created_at", "id", name="pk_webhook_deliveries"),
        # Best-effort same-instant dedupe guard (delivery is at-least-once; the Redis dispatch
        # marker collapses the common redelivery and receivers dedupe on the event id). The
        # partition key is included because a partitioned UNIQUE constraint requires it.
        sa.UniqueConstraint(
            "created_at", "subscription_id", "outbox_id", name="uq_webhook_deliveries_sub_outbox"
        ),
        sa.Index("webhook_deliveries_sub", "workspace_id", "subscription_id", "id"),  # log keyset
        sa.Index("webhook_deliveries_retry", "status", "next_attempt_at"),  # beat retry scan
        postgresql_partition_by="RANGE (created_at)",
    )
    op.execute("SELECT relay_ensure_partitions('webhook_deliveries', 2)")
    op.execute(_DROP_OLD_PARTITIONS)
    op.execute(_DUE_FUNCTION)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS relay_due_webhook_deliveries(int)")
    op.execute("DROP FUNCTION IF EXISTS relay_drop_old_partitions(text, int)")
    op.execute("DROP TABLE IF EXISTS webhook_deliveries CASCADE")  # drops partitions too
    op.drop_table("webhook_subscriptions")
