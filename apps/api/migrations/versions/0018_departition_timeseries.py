"""de-partition conversation_parts, events, webhook_deliveries → plain tables

Revision ID: 0018_departition_timeseries
Revises: 0017_device_tokens
Create Date: 2026-07-24

Reverses the monthly RANGE partitioning on the three append-only "firehose" tables
(``conversation_parts``, ``events``, ``webhook_deliveries``). At the product's current stage the
per-month child tables (``*_2026_07`` …) are premature optimization: they clutter the schema and
the auto-provisioning housekeeping adds moving parts for volume that doesn't exist yet. Each
becomes a plain tenant table with a simple ``id`` PK.

Safe: the tables are empty in every environment this ships to (fresh dev/test + a prod DB that was
only just migrated with zero rows), so ``DROP … CASCADE`` + recreate loses nothing. Nothing changes
functionally — every keyset cursor already keys on ``id`` alone (uuid7 time-ordered / bigint
identity), no FK targets these tables, and the composite ``(created_at, id)`` PKs existed *only*
because a partitioned PK must include the partition key. RLS, indexes, and columns port verbatim.

Retention: the O(1) ``DROP TABLE <old partition>`` path (``relay_drop_old_partitions``) is replaced
by a row-level ``DELETE`` retention function ``relay_purge_webhook_deliveries`` (SECURITY DEFINER,
BYPASSRLS owner, EXECUTE-granted to app_rw — same shape as before). The per-table
``*.ensure_partitions`` housekeeping tasks + their beat entries are removed in the app code.

When any of these tables genuinely approaches millions of rows/month, re-introduce partitioning for
*that* table deliberately (this migration's ``downgrade`` restores the partitioned shape).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from relay.core.rls import create_tenant_table

revision: str = "0018_departition_timeseries"
down_revision: str | None = "0017_device_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = pg.UUID(as_uuid=True)

# Row-level retention for webhook_deliveries (replaces the drop-old-partition path). SECURITY
# DEFINER + BYPASSRLS owner so the workspace-agnostic sweep sees every tenant; EXECUTE to app_rw so
# the housekeeping task calls it without DDL rights (mirrors the old relay_drop_old_partitions).
_PURGE_WEBHOOK_DELIVERIES = r"""
CREATE OR REPLACE FUNCTION relay_purge_webhook_deliveries(keep_days int)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $fn$
DECLARE deleted bigint;
BEGIN
    DELETE FROM public.webhook_deliveries
    WHERE created_at < now() - (keep_days || ' days')::interval;
    GET DIAGNOSTICS deleted = ROW_COUNT;
    RETURN deleted;
END;
$fn$;
REVOKE ALL ON FUNCTION relay_purge_webhook_deliveries(int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION relay_purge_webhook_deliveries(int) TO app_rw;
"""

# The retry-scan resolver — LANGUAGE sql, so it depends on webhook_deliveries and is dropped by the
# table's CASCADE. Recreated verbatim (from 0008) after the plain table exists.
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

# --- Partitioned-shape DDL, used ONLY by downgrade() to restore the original design -------------

_PARTITION_FUNCTIONS = r"""
CREATE OR REPLACE FUNCTION relay_create_month_partition(parent text, month_start date)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $fn$
DECLARE
    part_name text := parent || '_' || to_char(month_start, 'YYYY_MM');
    month_end  date := (month_start + interval '1 month')::date;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_class WHERE relname = part_name
        AND relnamespace = 'public'::regnamespace
    ) THEN
        EXECUTE format(
            'CREATE TABLE public.%I PARTITION OF public.%I FOR VALUES FROM (%L) TO (%L)',
            part_name, parent, month_start, month_end
        );
    END IF;
END;
$fn$;

CREATE OR REPLACE FUNCTION relay_ensure_partitions(parent text, months_ahead int)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $fn$
DECLARE m date := date_trunc('month', now())::date; i int;
BEGIN
    FOR i IN 0..months_ahead LOOP
        PERFORM relay_create_month_partition(parent, (m + (i || ' month')::interval)::date);
    END LOOP;
END;
$fn$;

CREATE OR REPLACE FUNCTION relay_missing_partitions(parent text, months_ahead int)
RETURNS TABLE(month date) LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public AS $fn$
DECLARE m date := date_trunc('month', now())::date; i int; part_name text;
BEGIN
    FOR i IN 0..months_ahead LOOP
        month := (m + (i || ' month')::interval)::date;
        part_name := parent || '_' || to_char(month, 'YYYY_MM');
        IF NOT EXISTS (
            SELECT 1 FROM pg_class WHERE relname = part_name
            AND relnamespace = 'public'::regnamespace
        ) THEN
            RETURN NEXT;
        END IF;
    END LOOP;
END;
$fn$;

REVOKE ALL ON FUNCTION relay_create_month_partition(text, date) FROM PUBLIC;
REVOKE ALL ON FUNCTION relay_ensure_partitions(text, int) FROM PUBLIC;
REVOKE ALL ON FUNCTION relay_missing_partitions(text, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION relay_ensure_partitions(text, int) TO app_rw;
GRANT EXECUTE ON FUNCTION relay_missing_partitions(text, int) TO app_rw;
"""

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
            CONTINUE;
        END;
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


def _create_conversation_parts(*, partitioned: bool) -> None:
    extra: dict[str, str] = {"postgresql_partition_by": "RANGE (created_at)"} if partitioned else {}
    pk = (
        sa.PrimaryKeyConstraint("created_at", "id", name="pk_conversation_parts")
        if partitioned
        else sa.PrimaryKeyConstraint("id", name="pk_conversation_parts")
    )
    create_tenant_table(
        "conversation_parts",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("workspace_id", _UUID, nullable=False),
        sa.Column("conversation_id", _UUID, nullable=False),
        sa.Column("author_kind", sa.Text(), nullable=False),
        sa.Column("author_id", _UUID, nullable=True),
        sa.Column("part_type", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column(
            "body_tsv",
            pg.TSVECTOR(),
            sa.Computed("to_tsvector('simple', coalesce(body, ''))", persisted=True),
            nullable=True,
        ),
        sa.Column("attachments", pg.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column(
            "channel_meta", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("meta", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        pk,
        sa.Index("parts_thread", "conversation_id", "id"),
        sa.Index("parts_fts", "body_tsv", postgresql_using="gin"),
        **extra,
    )


def _create_events(*, partitioned: bool) -> None:
    extra: dict[str, str] = {"postgresql_partition_by": "RANGE (created_at)"} if partitioned else {}
    pk = (
        sa.PrimaryKeyConstraint("created_at", "id", name="pk_events")
        if partitioned
        else sa.PrimaryKeyConstraint("id", name="pk_events")
    )
    create_tenant_table(
        "events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("workspace_id", _UUID, nullable=False),
        sa.Column("contact_id", _UUID, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("properties", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        pk,
        sa.Index("events_contact", "workspace_id", "contact_id", "name", "created_at"),
        sa.Index("events_brin", "created_at", postgresql_using="brin"),
        **extra,
    )


def _create_webhook_deliveries(*, partitioned: bool) -> None:
    extra: dict[str, str] = {"postgresql_partition_by": "RANGE (created_at)"} if partitioned else {}
    pk = (
        sa.PrimaryKeyConstraint("created_at", "id", name="pk_webhook_deliveries")
        if partitioned
        else sa.PrimaryKeyConstraint("id", name="pk_webhook_deliveries")
    )
    # Unique KEEPS ``created_at`` even when de-partitioned: it's a *same-instant* best-effort guard,
    # NOT an exactly-once key — a redispatch at a later instant must still create a row
    # (at-least-once; receivers dedupe on the stable event id). See webhooks.consumer docstring.
    uq = sa.UniqueConstraint(
        "created_at", "subscription_id", "outbox_id", name="uq_webhook_deliveries_sub_outbox"
    )
    create_tenant_table(
        "webhook_deliveries",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("workspace_id", _UUID, nullable=False),
        sa.Column("subscription_id", _UUID, nullable=False),
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
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        pk,
        uq,
        sa.Index("webhook_deliveries_sub", "workspace_id", "subscription_id", "id"),
        sa.Index("webhook_deliveries_retry", "status", "next_attempt_at"),
        **extra,
    )


def upgrade() -> None:
    # Drop the partitioned firehose tables (empty everywhere this ships) + their child partitions.
    # webhook_deliveries' CASCADE also drops the LANGUAGE-sql relay_due_webhook_deliveries.
    op.execute("DROP TABLE IF EXISTS conversation_parts CASCADE")
    op.execute("DROP TABLE IF EXISTS events CASCADE")
    op.execute("DROP TABLE IF EXISTS webhook_deliveries CASCADE")

    # Partition-management functions are now dead code.
    op.execute("DROP FUNCTION IF EXISTS relay_drop_old_partitions(text, int)")
    op.execute("DROP FUNCTION IF EXISTS relay_ensure_partitions(text, int)")
    op.execute("DROP FUNCTION IF EXISTS relay_missing_partitions(text, int)")
    op.execute("DROP FUNCTION IF EXISTS relay_create_month_partition(text, date)")

    # Recreate as plain tenant tables (RLS enabled + forced via create_tenant_table).
    _create_conversation_parts(partitioned=False)
    _create_events(partitioned=False)
    _create_webhook_deliveries(partitioned=False)

    # Restore the retry-scan resolver (dropped by the CASCADE) + the new row-level retention sweep.
    op.execute(_DUE_FUNCTION)
    op.execute(_PURGE_WEBHOOK_DELIVERIES)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS relay_purge_webhook_deliveries(int)")
    op.execute("DROP TABLE IF EXISTS conversation_parts CASCADE")
    op.execute("DROP TABLE IF EXISTS events CASCADE")
    op.execute("DROP TABLE IF EXISTS webhook_deliveries CASCADE")

    # Restore the partition-management functions, then the partitioned tables + seed T..T+2 months.
    op.execute(_PARTITION_FUNCTIONS)
    _create_conversation_parts(partitioned=True)
    op.execute("SELECT relay_ensure_partitions('conversation_parts', 2)")
    _create_events(partitioned=True)
    op.execute("SELECT relay_ensure_partitions('events', 2)")
    _create_webhook_deliveries(partitioned=True)
    op.execute("SELECT relay_ensure_partitions('webhook_deliveries', 2)")
    op.execute(_DROP_OLD_PARTITIONS)
    op.execute(_DUE_FUNCTION)
