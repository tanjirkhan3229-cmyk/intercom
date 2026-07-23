"""Celery tasks for the ``reporting`` module (P0.9).

- ``compute_daily_rollups`` (queue ``analytics``) — recompute ``daily_rollups`` for a target day
  (default: today **and** yesterday in UTC, so late closes/ratings on yesterday's conversations are
  picked up) from ``conversation_metrics`` via the ``relay_reporting_rollup`` SECURITY DEFINER
  function. **Idempotent**: the function's ``ON CONFLICT DO UPDATE`` recomputes the same values and
  preserves ``created_at``, so a re-run produces byte-identical rows (P0.9 acceptance).

Tasks are synchronous (Celery workers run sync); they use raw ``psycopg``. The rollup function is
owned by the BYPASSRLS ``migrator`` and EXECUTE-granted to ``app_rw``, so the ``app_rw`` task can
run the cross-workspace sweep without RLS getting in the way (mirrors the CRM/messaging housekeeping
functions).
"""

from __future__ import annotations

import datetime as dt

import psycopg

from relay.core.logging import get_logger
from relay.settings import get_settings
from relay.worker import celery_app

log = get_logger(__name__)


@celery_app.task(name="reporting.compute_daily_rollups", queue="analytics")
def compute_daily_rollups(day: str | None = None) -> dict[str, int]:
    """Recompute rollups for ``day`` (ISO ``YYYY-MM-DD``), or today + yesterday (UTC) by default.

    Returns the number of ``daily_rollups`` rows written per day. Idempotent — safe to re-run.
    """
    if day is not None:
        days = [dt.date.fromisoformat(day)]
    else:
        today = dt.datetime.now(dt.UTC).date()
        days = [today, today - dt.timedelta(days=1)]

    dsn = get_settings().database_url_psycopg
    written: dict[str, int] = {}
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        for target in days:
            cur.execute("SELECT relay_reporting_rollup(%s)", (target,))
            row = cur.fetchone()
            written[target.isoformat()] = int(row[0]) if row else 0
    log.info("reporting.rollups.computed", days=written)
    return written
