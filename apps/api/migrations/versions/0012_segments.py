"""segments: event_rollups + segments + segment_members + rollup/enumerate functions

Revision ID: 0012_segments
Revises: 0011_outbound
Create Date: 2026-07-24

P1.9 — RFC-002 §5.4 (segments, rollups). Three tenant tables (RLS enabled + FORCED via
``create_tenant_table``) plus two SECURITY DEFINER helpers owned by the BYPASSRLS ``migrator`` and
EXECUTE-granted to ``app_rw`` (mirrors ``relay_reporting_rollup``):

- ``relay_event_rollup(target_day)`` — idempotent full-day recompute of ``event_rollups`` from the
  ``events`` partitions (``GROUP BY`` + ``ON CONFLICT DO UPDATE`` + delete-absent). The day range is
  expressed as sargable ``created_at`` bounds so the events partitions prune.
- ``relay_all_segments()`` — enumerate ``(workspace_id, segment_id)`` for the nightly reconcile (a
  workspace-agnostic sweep can't run under ``app_rw``'s forced RLS).

Index strategy vs scripts/check_migrations.py:
- ``event_rollups`` + ``segment_members`` are in **LARGE_TABLES**. Their natural-key PK is declared
  inline on CREATE TABLE (the linter only scans ``op.create_index``/``op.execute``); the secondary
  indexes are built ``CONCURRENTLY`` inside an ``autocommit_block`` (the large-table pattern,
  mirroring ``contacts`` in 0002_crm / ``sends`` in 0011_outbound).
- ``segments`` is small/regular: plain ``op.create_index``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from relay.core.rls import create_tenant_table

revision: str = "0012_segments"
down_revision: str | None = "0011_outbound"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = pg.UUID(as_uuid=True)


def _id_col() -> sa.Column:
    return sa.Column("id", _UUID, primary_key=True)


def _created_at_col() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def _updated_at_col() -> sa.Column:
    return sa.Column(
        "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def _workspace_fk() -> sa.Column:
    return sa.Column(
        "workspace_id", _UUID, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )


# Idempotent per-UTC-day event rollup (SECURITY DEFINER — the sweep spans all workspaces). The day
# is bounded as sargable timestamptz range so the ``events`` monthly partitions prune; every row in
# the range is by construction on ``target_day``, so ``day`` is inserted as the literal.
_ROLLUP_FUNCTION = r"""
CREATE OR REPLACE FUNCTION relay_event_rollup(target_day date)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $fn$
DECLARE affected bigint;
BEGIN
    WITH agg AS (
        SELECT workspace_id, contact_id, name AS event_name, count(*) AS cnt
        FROM public.events
        WHERE created_at >= (target_day::timestamp AT TIME ZONE 'UTC')
          AND created_at <  ((target_day + 1)::timestamp AT TIME ZONE 'UTC')
        GROUP BY workspace_id, contact_id, name
    ),
    -- Authoritative for target_day: drop rollup rows whose grain no longer has events that day
    -- (e.g. events scrubbed for GDPR). Disjoint from the upsert's row set, so both CTEs are safe.
    del AS (
        DELETE FROM public.event_rollups r
        WHERE r.day = target_day
          AND NOT EXISTS (
              SELECT 1 FROM agg a
              WHERE a.workspace_id = r.workspace_id
                AND a.contact_id = r.contact_id
                AND a.event_name = r.event_name
          )
        RETURNING 1
    ),
    ups AS (
        INSERT INTO public.event_rollups
            (workspace_id, contact_id, event_name, day, count, updated_at)
        SELECT workspace_id, contact_id, event_name, target_day, cnt, now() FROM agg
        ON CONFLICT (workspace_id, contact_id, event_name, day)
            DO UPDATE SET count = EXCLUDED.count, updated_at = now()
        RETURNING 1
    )
    SELECT count(*) INTO affected FROM ups;
    RETURN affected;
END;
$fn$;
REVOKE ALL ON FUNCTION relay_event_rollup(date) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION relay_event_rollup(date) TO app_rw;
"""

# Enumerate all segments for the nightly reconcile (SECURITY DEFINER — cross-workspace sweep).
_ALL_SEGMENTS_FUNCTION = r"""
CREATE OR REPLACE FUNCTION relay_all_segments()
RETURNS TABLE(workspace_id uuid, segment_id uuid)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public AS $fn$
    SELECT workspace_id, id FROM public.segments
$fn$;
REVOKE ALL ON FUNCTION relay_all_segments() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION relay_all_segments() TO app_rw;
"""


def upgrade() -> None:
    # --- event_rollups (LARGE; natural-key PK; feeds segment event-count predicates) ---
    create_tenant_table(
        "event_rollups",
        _workspace_fk(),
        sa.Column("contact_id", _UUID, nullable=False),
        sa.Column("event_name", sa.Text(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("count", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint(
            "workspace_id", "contact_id", "event_name", "day", name="pk_event_rollups"
        ),
    )

    # --- segments (small; the named audience definition) ---
    create_tenant_table(
        "segments",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("predicate", pg.JSONB(), nullable=False),
        sa.Column(
            "cached_member_count", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("member_count_updated_at", sa.DateTime(timezone=True), nullable=True),
        _updated_at_col(),
        sa.UniqueConstraint("workspace_id", "name", name="uq_segments_workspace_id_name"),
    )
    op.create_index("ix_segments_ws_id", "segments", ["workspace_id", "id"])

    # --- segment_members (LARGE; natural-key PK; materialised membership) ---
    create_tenant_table(
        "segment_members",
        _workspace_fk(),
        sa.Column(
            "segment_id", _UUID, sa.ForeignKey("segments.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "contact_id", _UUID, sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint(
            "workspace_id", "segment_id", "contact_id", name="pk_segment_members"
        ),
    )

    # Large-table secondary indexes: CONCURRENTLY (tables are empty now, so this is instant, and it
    # establishes the enforced pattern the linter requires for these LARGE_TABLES).
    with op.get_context().autocommit_block():
        # Segment event-count subquery filters by (workspace_id, event_name, day) across contacts.
        op.create_index(
            "ix_event_rollups_ws_name_day",
            "event_rollups",
            ["workspace_id", "event_name", "day"],
            postgresql_concurrently=True,
        )
        # Delta path deletes/looks up membership by contact.
        op.create_index(
            "ix_segment_members_ws_contact",
            "segment_members",
            ["workspace_id", "contact_id"],
            postgresql_concurrently=True,
        )

    op.execute(_ROLLUP_FUNCTION)
    op.execute(_ALL_SEGMENTS_FUNCTION)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS relay_all_segments()")
    op.execute("DROP FUNCTION IF EXISTS relay_event_rollup(date)")
    op.drop_table("segment_members")
    op.drop_table("segments")
    op.drop_table("event_rollups")
