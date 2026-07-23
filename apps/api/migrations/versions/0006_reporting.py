"""reporting: conversation_metrics, daily_rollups + the idempotent rollup function

Revision ID: 0006_reporting
Revises: 0005_merge_billing_knowledge
Create Date: 2026-07-23

RFC-000 §2.9 + RFC-002 §5.6 (reporting tables) + §2 R4/R9. Both tables are tenant tables
(RLS enabled + FORCED via ``create_tenant_table``).

The rollup is a SECURITY DEFINER function ``relay_reporting_rollup(target_day)`` owned by the
BYPASSRLS ``migrator`` and EXECUTE-granted to ``app_rw`` (mirrors the purge / partition
functions): it aggregates every workspace's ``conversation_metrics`` into
``daily_rollups`` in one statement — a workspace-agnostic sweep can't run under ``app_rw``'s forced
RLS. It is idempotent (``ON CONFLICT … DO UPDATE`` recomputes the same values; ``created_at`` is
preserved, so a re-run yields byte-identical rows — P0.9 acceptance).

Index note (vs scripts/check_migrations.py): neither table is in LARGE_TABLES, so plain
``op.create_index`` on the (empty) new tables is fine — no CONCURRENTLY required.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from relay.core.rls import create_tenant_table

revision: str = "0006_reporting"
down_revision: str | None = "0005_merge_billing_knowledge"
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


# Idempotent daily rollup across all workspaces (SECURITY DEFINER — bypasses RLS for the sweep).
# Days are bucketed in UTC so the boundary is deterministic regardless of session TimeZone.
_ROLLUP_FUNCTION = r"""
CREATE OR REPLACE FUNCTION relay_reporting_rollup(target_day date)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $fn$
DECLARE affected bigint;
BEGIN
    -- Each fact is bucketed by the day it HAPPENED (all in UTC): opens by opened_at, closes by
    -- closed_at, CSAT by rated_at. Bucketing ratings by rated_at (not opened_at) is essential —
    -- a customer often rates days after the conversation opened, and the scheduled rollup only
    -- refreshes today+yesterday, so an open-day bucket would never pick the late rating up.
    -- team_id is the conversation's first-observed team (latched once by the reducer, then
    -- immutable), so a later reassignment can't move a conversation between buckets.
    WITH opened_agg AS (
        SELECT workspace_id, team_id,
            count(*) AS conversations_opened,
            count(first_response_s) AS first_response_count,
            coalesce(sum(first_response_s), 0) AS first_response_sum_s,
            coalesce(sum(replies_count), 0) AS replies_count
        FROM public.conversation_metrics
        WHERE (opened_at AT TIME ZONE 'UTC')::date = target_day
        GROUP BY workspace_id, team_id
    ),
    closed_agg AS (
        SELECT workspace_id, team_id, count(*) AS conversations_closed
        FROM public.conversation_metrics
        WHERE (closed_at AT TIME ZONE 'UTC')::date = target_day
        GROUP BY workspace_id, team_id
    ),
    rated_agg AS (
        SELECT workspace_id, team_id,
            count(rating) AS rating_count,
            coalesce(sum(rating), 0) AS rating_sum
        FROM public.conversation_metrics
        WHERE (rated_at AT TIME ZONE 'UTC')::date = target_day AND rating IS NOT NULL
        GROUP BY workspace_id, team_id
    ),
    rated_hist AS (
        SELECT workspace_id, team_id, jsonb_object_agg(rating::text, cnt) AS rating_histogram
        FROM (
            SELECT workspace_id, team_id, rating, count(*) AS cnt
            FROM public.conversation_metrics
            WHERE (rated_at AT TIME ZONE 'UTC')::date = target_day AND rating IS NOT NULL
            GROUP BY workspace_id, team_id, rating
        ) r GROUP BY workspace_id, team_id
    ),
    -- The universe of (workspace, team) buckets touched on this day, across all three facts.
    -- UNION treats NULL teams as equal, so the unassigned bucket appears once per workspace.
    keys AS (
        SELECT workspace_id, team_id FROM opened_agg
        UNION SELECT workspace_id, team_id FROM closed_agg
        UNION SELECT workspace_id, team_id FROM rated_agg
    ),
    combined AS (
        SELECT k.workspace_id, k.team_id,
            coalesce(o.conversations_opened, 0) AS conversations_opened,
            coalesce(cl.conversations_closed, 0) AS conversations_closed,
            coalesce(o.replies_count, 0) AS replies_count,
            coalesce(o.first_response_count, 0) AS first_response_count,
            coalesce(o.first_response_sum_s, 0) AS first_response_sum_s,
            coalesce(ra.rating_count, 0) AS rating_count,
            coalesce(ra.rating_sum, 0) AS rating_sum,
            coalesce(h.rating_histogram, '{}'::jsonb) AS rating_histogram
        FROM keys k
        LEFT JOIN opened_agg o
            ON o.workspace_id = k.workspace_id AND o.team_id IS NOT DISTINCT FROM k.team_id
        LEFT JOIN closed_agg cl
            ON cl.workspace_id = k.workspace_id AND cl.team_id IS NOT DISTINCT FROM k.team_id
        LEFT JOIN rated_agg ra
            ON ra.workspace_id = k.workspace_id AND ra.team_id IS NOT DISTINCT FROM k.team_id
        LEFT JOIN rated_hist h
            ON h.workspace_id = k.workspace_id AND h.team_id IS NOT DISTINCT FROM k.team_id
    ),
    -- The rollup is AUTHORITATIVE for target_day: drop any (workspace, team) bucket that no longer
    -- has activity on this day (e.g. the underlying conversation was deleted, or — defensively — a
    -- team correction moved it). ``IS NOT DISTINCT FROM`` so the team_id IS NULL bucket matches.
    -- Runs on the same snapshot as the upsert but on a DISJOINT set of rows (buckets absent from
    -- ``combined``), so the two data-modifying CTEs never touch the same row. It executes to
    -- completion even though the final SELECT reads only ``upsert``.
    deleted AS (
        DELETE FROM public.daily_rollups d
        WHERE d.day = target_day
          AND NOT EXISTS (
              SELECT 1 FROM combined c
              WHERE c.workspace_id = d.workspace_id
                AND c.team_id IS NOT DISTINCT FROM d.team_id
          )
        RETURNING 1
    ),
    upsert AS (
        INSERT INTO public.daily_rollups (
            id, workspace_id, created_at, day, team_id,
            conversations_opened, conversations_closed, replies_count,
            first_response_count, first_response_sum_s, rating_count, rating_sum, rating_histogram
        )
        SELECT gen_random_uuid(), workspace_id, now(), target_day, team_id,
            conversations_opened, conversations_closed, replies_count,
            first_response_count, first_response_sum_s, rating_count, rating_sum, rating_histogram
        FROM combined
        ON CONFLICT (workspace_id, day, team_id) DO UPDATE SET
            conversations_opened = EXCLUDED.conversations_opened,
            conversations_closed = EXCLUDED.conversations_closed,
            replies_count = EXCLUDED.replies_count,
            first_response_count = EXCLUDED.first_response_count,
            first_response_sum_s = EXCLUDED.first_response_sum_s,
            rating_count = EXCLUDED.rating_count,
            rating_sum = EXCLUDED.rating_sum,
            rating_histogram = EXCLUDED.rating_histogram
        RETURNING 1
    )
    SELECT count(*) INTO affected FROM upsert;
    RETURN affected;
END;
$fn$;
REVOKE ALL ON FUNCTION relay_reporting_rollup(date) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION relay_reporting_rollup(date) TO app_rw;
"""


def upgrade() -> None:
    # --- conversation_metrics (one upserted row per conversation, folded from outbox events) ---
    create_tenant_table(
        "conversation_metrics",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column(
            "conversation_id",
            _UUID,
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("team_id", _UUID, nullable=True),
        sa.Column("assignee_id", _UUID, nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_admin_reply_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_response_s", sa.Integer(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_s", sa.Integer(), nullable=True),
        sa.Column("reopen_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("replies_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("rated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seq", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.UniqueConstraint(
            "workspace_id",
            "conversation_id",
            name="uq_conversation_metrics_workspace_id_conversation_id",
        ),
    )
    # Responsiveness + volume read paths (opened-day) and closed counts; all lead with workspace_id.
    op.create_index(
        "ix_conversation_metrics_ws_opened", "conversation_metrics", ["workspace_id", "opened_at"]
    )
    op.create_index(
        "ix_conversation_metrics_ws_team_opened",
        "conversation_metrics",
        ["workspace_id", "team_id", "opened_at"],
    )
    op.create_index(
        "ix_conversation_metrics_ws_closed", "conversation_metrics", ["workspace_id", "closed_at"]
    )

    # --- daily_rollups (per workspace/day/team; recomputed idempotently by the rollup function) ---
    create_tenant_table(
        "daily_rollups",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("team_id", _UUID, nullable=True),
        sa.Column(
            "conversations_opened", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "conversations_closed", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("replies_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "first_response_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "first_response_sum_s", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("rating_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("rating_sum", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "rating_histogram", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        # NULLS NOT DISTINCT (PG15+): the team_id IS NULL bucket is a single ON CONFLICT target.
        sa.UniqueConstraint(
            "workspace_id",
            "day",
            "team_id",
            name="uq_daily_rollups_workspace_id_day_team_id",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index("ix_daily_rollups_ws_day", "daily_rollups", ["workspace_id", "day"])

    op.execute(_ROLLUP_FUNCTION)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS relay_reporting_rollup(date)")
    op.drop_table("daily_rollups")
    op.drop_table("conversation_metrics")
