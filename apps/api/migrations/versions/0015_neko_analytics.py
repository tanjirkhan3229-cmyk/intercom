"""neko analytics: ai_involved flag + neko_daily_rollups + relay_neko_rollup

Revision ID: 0015_neko_analytics
Revises: 0014_integrations
Create Date: 2026-07-24

P1.4 — the Neko analytics reporting spine (RFC-003 §8 analytics, RFC-002 §5.6). Expand-only
(master rule 4): a new nullable-with-default column, a new tenant table, a new SECURITY DEFINER
rollup function, and a plain index on the (small) ``agent_runs`` table.

- ``conversation_metrics.ai_involved`` — the ``ai_involved`` flag RFC-002 §5.6 specifies but P0.9
  did not build. Folded by the ``reporting-metrics`` reducer (set true when Neko authors a part).
  It powers the **CSAT delta** (Neko-touched vs not) — a query over ``conversation_metrics`` (the
  projection), never over parts.
- ``neko_daily_rollups`` — per ``(workspace_id, day)`` Neko aggregate, recomputed idempotently by
  ``relay_neko_rollup`` from ``agent_runs`` (+ ``usage_records`` for the billing-grade resolution
  count). The analytics dashboards read THIS, never scan raw ``agent_runs`` (P1.4 acceptance).
- ``relay_neko_rollup(target_day)`` — SECURITY DEFINER (bypasses RLS for the cross-workspace sweep),
  owned by ``migrator`` and EXECUTE-granted to ``app_rw`` — the exact shape of
  ``relay_reporting_rollup`` (0006). ``resolutions`` is ``SUM(usage_records.qty)`` for the
  ``ai_resolution`` meter, so the analytics figure reconciles with billing net-of-claw-back.
- ``ix_agent_runs_ws_id`` — the run-inspector's workspace-wide keyset list
  (``WHERE workspace_id=? [filters] ORDER BY id DESC``). ``agent_runs`` is not a LARGE_TABLE, so a
  plain (non-CONCURRENTLY) index is fine (scripts/check_migrations.py).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from relay.core.rls import create_tenant_table

revision: str = "0015_neko_analytics"
down_revision: str | None = "0014_integrations"
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


def _int0(name: str) -> sa.Column:
    return sa.Column(name, sa.Integer(), nullable=False, server_default=sa.text("0"))


# Idempotent daily Neko rollup across all workspaces (SECURITY DEFINER — bypasses RLS for the
# sweep). Mirrors relay_reporting_rollup (0006): bucket each fact by the UTC day it HAPPENED, union
# the touched workspaces, delete stale buckets, then ON CONFLICT DO UPDATE (byte-identical re-runs).
# ``resolutions`` comes from usage_records (the billing meter) so the analytics number is net of
# claw-backs and reconciles with what Stripe is billed (RFC-003 §8).
_ROLLUP_FUNCTION = r"""
CREATE OR REPLACE FUNCTION relay_neko_rollup(target_day date)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $fn$
DECLARE affected bigint;
BEGIN
    WITH run_agg AS (
        SELECT workspace_id,
            count(*) AS runs_total,
            count(*) FILTER (WHERE outcome = 'answered')   AS runs_answered,
            count(*) FILTER (WHERE outcome = 'clarify')     AS runs_clarify,
            count(*) FILTER (WHERE outcome = 'handoff')     AS runs_handoff,
            count(*) FILTER (WHERE outcome = 'ineligible')  AS runs_ineligible,
            count(*) FILTER (WHERE outcome = 'error')       AS runs_error,
            count(DISTINCT conversation_id) AS conversations_engaged,
            count(DISTINCT conversation_id) FILTER (WHERE outcome = 'answered')
                AS conversations_answered,
            count(DISTINCT conversation_id) FILTER (WHERE outcome = 'handoff')
                AS conversations_handoff,
            coalesce(sum(cost_usd), 0) AS cost_usd_sum,
            coalesce(sum((latency_ms->>'total')::double precision)
                     FILTER (WHERE latency_ms ? 'total'), 0) AS latency_ms_sum,
            count(*) FILTER (WHERE latency_ms ? 'total') AS latency_count
        FROM public.agent_runs
        WHERE status = 'complete' AND (created_at AT TIME ZONE 'UTC')::date = target_day
        GROUP BY workspace_id
    ),
    -- Handoff-reason histogram (RFC-003 §8). NULL reason -> 'unspecified'.
    handoff_hist AS (
        SELECT workspace_id, jsonb_object_agg(reason, cnt) AS handoff_reasons
        FROM (
            SELECT workspace_id, coalesce(handoff_reason, 'unspecified') AS reason, count(*) AS cnt
            FROM public.agent_runs
            WHERE status = 'complete' AND outcome = 'handoff'
              AND (created_at AT TIME ZONE 'UTC')::date = target_day
            GROUP BY workspace_id, coalesce(handoff_reason, 'unspecified')
        ) h GROUP BY workspace_id
    ),
    -- Billing-grade resolutions: net SUM(qty) of the ai_resolution meter, bucketed by occurred_at.
    -- A claw-back (negative row) lands on the reopen day, so a day can be net-negative — correct;
    -- the window total still reconciles with billing's net_usage_in_period.
    res_agg AS (
        SELECT workspace_id, coalesce(sum(qty), 0) AS resolutions
        FROM public.usage_records
        WHERE meter = 'ai_resolution' AND (occurred_at AT TIME ZONE 'UTC')::date = target_day
        GROUP BY workspace_id
    ),
    keys AS (
        SELECT workspace_id FROM run_agg
        UNION SELECT workspace_id FROM res_agg
    ),
    combined AS (
        SELECT k.workspace_id,
            coalesce(r.runs_total, 0)              AS runs_total,
            coalesce(r.runs_answered, 0)           AS runs_answered,
            coalesce(r.runs_clarify, 0)            AS runs_clarify,
            coalesce(r.runs_handoff, 0)            AS runs_handoff,
            coalesce(r.runs_ineligible, 0)         AS runs_ineligible,
            coalesce(r.runs_error, 0)              AS runs_error,
            coalesce(r.conversations_engaged, 0)   AS conversations_engaged,
            coalesce(r.conversations_answered, 0)  AS conversations_answered,
            coalesce(r.conversations_handoff, 0)   AS conversations_handoff,
            coalesce(r.cost_usd_sum, 0)            AS cost_usd_sum,
            coalesce(r.latency_ms_sum, 0)          AS latency_ms_sum,
            coalesce(r.latency_count, 0)           AS latency_count,
            coalesce(h.handoff_reasons, '{}'::jsonb) AS handoff_reasons,
            coalesce(rs.resolutions, 0)            AS resolutions
        FROM keys k
        LEFT JOIN run_agg r      ON r.workspace_id  = k.workspace_id
        LEFT JOIN handoff_hist h ON h.workspace_id  = k.workspace_id
        LEFT JOIN res_agg rs     ON rs.workspace_id = k.workspace_id
    ),
    -- Authoritative for target_day: drop buckets with no activity left on this day (disjoint from
    -- the upsert's row set, so the two data-modifying CTEs never touch the same row).
    deleted AS (
        DELETE FROM public.neko_daily_rollups d
        WHERE d.day = target_day
          AND NOT EXISTS (SELECT 1 FROM combined c WHERE c.workspace_id = d.workspace_id)
        RETURNING 1
    ),
    upsert AS (
        INSERT INTO public.neko_daily_rollups (
            id, workspace_id, created_at, day,
            runs_total, runs_answered, runs_clarify, runs_handoff, runs_ineligible, runs_error,
            conversations_engaged, conversations_answered, conversations_handoff,
            cost_usd_sum, latency_ms_sum, latency_count, handoff_reasons, resolutions
        )
        SELECT gen_random_uuid(), workspace_id, now(), target_day,
            runs_total, runs_answered, runs_clarify, runs_handoff, runs_ineligible, runs_error,
            conversations_engaged, conversations_answered, conversations_handoff,
            cost_usd_sum, latency_ms_sum, latency_count, handoff_reasons, resolutions
        FROM combined
        ON CONFLICT (workspace_id, day) DO UPDATE SET
            runs_total             = EXCLUDED.runs_total,
            runs_answered          = EXCLUDED.runs_answered,
            runs_clarify           = EXCLUDED.runs_clarify,
            runs_handoff           = EXCLUDED.runs_handoff,
            runs_ineligible        = EXCLUDED.runs_ineligible,
            runs_error             = EXCLUDED.runs_error,
            conversations_engaged  = EXCLUDED.conversations_engaged,
            conversations_answered = EXCLUDED.conversations_answered,
            conversations_handoff  = EXCLUDED.conversations_handoff,
            cost_usd_sum           = EXCLUDED.cost_usd_sum,
            latency_ms_sum         = EXCLUDED.latency_ms_sum,
            latency_count          = EXCLUDED.latency_count,
            handoff_reasons        = EXCLUDED.handoff_reasons,
            resolutions            = EXCLUDED.resolutions
        RETURNING 1
    )
    SELECT count(*) INTO affected FROM upsert;
    RETURN affected;
END;
$fn$;
REVOKE ALL ON FUNCTION relay_neko_rollup(date) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION relay_neko_rollup(date) TO app_rw;
"""


def upgrade() -> None:
    # --- conversation_metrics.ai_involved (RFC-002 §5.6) ----------------------------------------
    # Nullable-with-default add: a cheap metadata-only change on PG11+ (expand-only, rule 4).
    op.add_column(
        "conversation_metrics",
        sa.Column("ai_involved", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    # --- neko_daily_rollups (per workspace/day Neko aggregate; recomputed idempotently) ---------
    create_tenant_table(
        "neko_daily_rollups",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("day", sa.Date(), nullable=False),
        _int0("runs_total"),
        _int0("runs_answered"),
        _int0("runs_clarify"),
        _int0("runs_handoff"),
        _int0("runs_ineligible"),
        _int0("runs_error"),
        _int0("conversations_engaged"),
        _int0("conversations_answered"),
        _int0("conversations_handoff"),
        sa.Column("cost_usd_sum", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("latency_ms_sum", sa.Float(), nullable=False, server_default=sa.text("0")),
        _int0("latency_count"),
        sa.Column(
            "handoff_reasons", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        # Numeric (like usage_records.qty): billing reconciliation is exact; net of claw-backs.
        sa.Column("resolutions", sa.Numeric(), nullable=False, server_default=sa.text("0")),
        sa.UniqueConstraint("workspace_id", "day", name="uq_neko_daily_rollups_workspace_id_day"),
    )
    op.create_index("ix_neko_daily_rollups_ws_day", "neko_daily_rollups", ["workspace_id", "day"])

    op.execute(_ROLLUP_FUNCTION)

    # --- run-inspector read path (P1.4): workspace-wide keyset list ORDER BY id DESC ------------
    op.create_index("ix_agent_runs_ws_id", "agent_runs", ["workspace_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_agent_runs_ws_id", table_name="agent_runs")
    op.execute("DROP FUNCTION IF EXISTS relay_neko_rollup(date)")
    op.drop_index("ix_neko_daily_rollups_ws_day", table_name="neko_daily_rollups")
    op.drop_table("neko_daily_rollups")
    op.drop_column("conversation_metrics", "ai_involved")
