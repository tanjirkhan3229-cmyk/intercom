"""crm: contacts, companies, contact_companies, attribute_definitions, events + partitions

Revision ID: 0002_crm
Revises: 0001_identity
Create Date: 2026-07-23

RFC-002 §5.4. All tables are tenant tables (RLS enabled + FORCED via ``create_tenant_table``).

Index strategy vs the migration linter (scripts/check_migrations.py, which requires
``CONCURRENTLY`` on large tables):
- ``contacts`` (large, regular): indexes are built ``CONCURRENTLY`` inside an
  ``autocommit_block`` (the blessed pattern; empty table here, but the linter enforces it).
- ``events`` (large, *partitioned*): ``CREATE INDEX CONCURRENTLY`` is unsupported on a
  partitioned parent, so its indexes are declared inline on ``create_table`` as partitioned
  index *templates* (auto-applied to every partition). Inline indexes are emitted by the DDL
  compiler, not via ``op.create_index``/``op.execute``, so the linter (which scans those
  calls) does not flag them — and there is no lock concern on a brand-new empty table.

Monthly partitions are managed by SECURITY DEFINER functions owned by the BYPASSRLS
``migrator`` and EXECUTE-granted to ``app_rw`` (the ``housekeeping`` task calls them at
runtime; app_rw never needs DDL). The migration seeds current + 2 months ahead.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from relay.core.rls import create_tenant_table

revision: str = "0002_crm"
down_revision: str | None = "0001_identity"
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


# --- Partition-management functions (SECURITY DEFINER, owned by migrator) ------

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


def upgrade() -> None:
    # --- contacts (large, regular) ---
    create_tenant_table(
        "contacts",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("kind", sa.Text(), nullable=False, server_default=sa.text("'user'")),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("email", pg.CITEXT(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("custom", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('user', 'lead')", name="ck_contacts_kind_valid"),
    )

    # --- companies ---
    create_tenant_table(
        "companies",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("domain", sa.Text(), nullable=True),
        sa.Column("custom", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index(
        "companies_ext",
        "companies",
        ["workspace_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )
    op.create_index("ix_companies_ws_id", "companies", ["workspace_id", "id"])
    # Composite GIN (btree_gin) leading with workspace_id: under FORCED RLS the leakproof
    # workspace_id equality is the usable index cond; ILIKE (non-leakproof) is a filter. A
    # bare gin(name) index is never used under RLS. Leads with workspace_id per RFC-002 §5.1.
    op.create_index(
        "companies_name_trgm",
        "companies",
        ["workspace_id", "name"],
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )

    # --- contact_companies (join) ---
    create_tenant_table(
        "contact_companies",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column(
            "contact_id", _UUID, sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "company_id", _UUID, sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "contact_id",
            "company_id",
            name="uq_contact_companies_contact_company",
        ),
    )
    op.create_index(
        "ix_contact_companies_company", "contact_companies", ["workspace_id", "company_id"]
    )

    # --- attribute_definitions (the custom-JSONB schema) ---
    create_tenant_table(
        "attribute_definitions",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("entity", sa.Text(), nullable=False, server_default=sa.text("'contact'")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("data_type", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "workspace_id", "entity", "name", name="uq_attribute_definitions_entity_name"
        ),
        sa.CheckConstraint(
            "data_type IN ('string', 'number', 'boolean', 'date', 'list')",
            name="ck_attribute_definitions_data_type_valid",
        ),
        sa.CheckConstraint(
            "entity IN ('contact', 'company')", name="ck_attribute_definitions_entity_valid"
        ),
    )

    # --- events (large, partitioned firehose) — inline index templates ---
    create_tenant_table(
        "events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("workspace_id", _UUID, nullable=False),
        sa.Column("contact_id", _UUID, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("properties", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("created_at", "id", name="pk_events"),
        sa.Index("events_contact", "workspace_id", "contact_id", "name", "created_at"),
        sa.Index("events_brin", "created_at", postgresql_using="brin"),
        postgresql_partition_by="RANGE (created_at)",
    )

    # Partition automation + seed current..T+2 months.
    op.execute(_PARTITION_FUNCTIONS)
    op.execute("SELECT relay_ensure_partitions('events', 2)")

    # --- contacts indexes: CONCURRENTLY (large table; linter-enforced) ---
    with op.get_context().autocommit_block():
        op.create_index(
            "contacts_ext",
            "contacts",
            ["workspace_id", "external_id"],
            unique=True,
            postgresql_where=sa.text("external_id IS NOT NULL AND deleted_at IS NULL"),
            postgresql_concurrently=True,
        )
        op.create_index(
            "contacts_email_user",
            "contacts",
            ["workspace_id", "email"],
            unique=True,
            postgresql_where=sa.text("kind = 'user' AND email IS NOT NULL AND deleted_at IS NULL"),
            postgresql_concurrently=True,
        )
        # Composite GIN (btree_gin) leading with workspace_id (RFC-002 §5.1 convention).
        # Under FORCED RLS, ILIKE (`~~*`) is not leakproof, so it is applied as a filter and
        # a bare gin(name) index is never chosen; the leakproof workspace_id equality is the
        # index cond, so this composite is what serves R8 typeahead (no Seq Scan).
        op.create_index(
            "contacts_name_trgm",
            "contacts",
            ["workspace_id", "name"],
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
            postgresql_concurrently=True,
        )
        op.create_index(
            "contacts_custom",
            "contacts",
            ["custom"],
            postgresql_using="gin",
            postgresql_ops={"custom": "jsonb_path_ops"},
            postgresql_concurrently=True,
        )
        op.create_index(
            "contacts_ws_active",
            "contacts",
            ["workspace_id", "id"],
            postgresql_where=sa.text("deleted_at IS NULL"),
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    op.drop_table("contact_companies")
    op.drop_table("contacts")
    op.drop_table("companies")
    op.drop_table("attribute_definitions")
    op.execute("DROP TABLE IF EXISTS events CASCADE")  # drops partitions too
    op.execute("DROP FUNCTION IF EXISTS relay_ensure_partitions(text, int)")
    op.execute("DROP FUNCTION IF EXISTS relay_missing_partitions(text, int)")
    op.execute("DROP FUNCTION IF EXISTS relay_create_month_partition(text, date)")
