"""Celery tasks for the ``billing`` module (RFC-002 §5.6 — seats synced daily + on change).

Tasks are synchronous (Celery workers run sync) and use raw ``psycopg`` + the sync Stripe
client, like the CRM analytics drain (``relay.modules.crm.tasks``). ``workspaces`` is a
global (no-RLS) table, so both tasks loop over every workspace id, set ``app.ws`` for that
one workspace, and only then touch tenant tables (``subscriptions``, ``memberships``) — the
same per-workspace-GUC pattern the CRM drain uses for its cross-tenant sweep.

- ``recalculate_all_seats`` (daily, ``housekeeping``) — full reconciliation: recomputes every
  workspace's seat count from active memberships. Catches anything the on-change path missed.
- ``sync_seats_to_stripe`` (every 5 min, ``housekeeping``) — pushes only *dirty* rows (where
  ``seats != seats_stripe_synced``) to Stripe. This is the only place a Stripe write for seats
  happens — never on the request path (master rule 5 / RFC-001 §5).

Every task is idempotent: re-running either is a no-op once ``seats == seats_stripe_synced``.
"""

from __future__ import annotations

import datetime as dt
import json

import psycopg

from relay.core.ids import uuid7
from relay.core.logging import get_logger
from relay.settings import get_settings
from relay.worker import celery_app

from . import events
from .service import RESOLUTION_METER
from .stripe_client import create_meter_event_sync, update_subscription_item_quantity_sync

log = get_logger(__name__)

_SELECT_WORKSPACE_IDS = "SELECT id FROM workspaces"
_SET_GUC = "SELECT set_config('app.ws', %s, true)"
_SELECT_SUBSCRIPTION = (
    "SELECT id, seats, seats_stripe_synced, stripe_subscription_item_id "
    "FROM subscriptions WHERE workspace_id = %s"
)
_COUNT_ACTIVE_MEMBERSHIPS = (
    "SELECT count(*) FROM memberships m JOIN admins a ON a.id = m.admin_id "
    "WHERE m.workspace_id = %s AND a.is_active = true"
)
_UPDATE_SEATS = "UPDATE subscriptions SET seats = %s WHERE id = %s"
_UPDATE_SEATS_STRIPE_SYNCED = "UPDATE subscriptions SET seats_stripe_synced = %s WHERE id = %s"
_INSERT_OUTBOX = (
    "INSERT INTO outbox (id, aggregate, aggregate_id, seq, topic, payload) "
    "VALUES (%s, 'subscription', %s, "
    "(SELECT coalesce(max(seq), 0) + 1 FROM outbox WHERE aggregate_id = %s), %s, %s)"
)
_NOTIFY = "NOTIFY relay_outbox"


def _iter_workspace_ids(conn: psycopg.Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(_SELECT_WORKSPACE_IDS)
        return [str(row[0]) for row in cur.fetchall()]


@celery_app.task(name="billing.recalculate_all_seats", queue="housekeeping")
def recalculate_all_seats() -> int:
    """Daily full reconciliation: recompute every workspace's seat count. Returns rows changed."""
    dsn = get_settings().database_url_psycopg
    changed = 0
    with psycopg.connect(dsn) as conn:
        conn.autocommit = False
        for workspace_id in _iter_workspace_ids(conn):
            with conn.cursor() as cur:
                cur.execute(_SET_GUC, (workspace_id,))
                cur.execute(_SELECT_SUBSCRIPTION, (workspace_id,))
                row = cur.fetchone()
                if row is None:
                    conn.commit()
                    continue
                sub_id, seats, _seats_stripe_synced, _item_id = row
                cur.execute(_COUNT_ACTIVE_MEMBERSHIPS, (workspace_id,))
                count_row = cur.fetchone()
                assert count_row is not None
                (count,) = count_row
                if count != seats:
                    cur.execute(_UPDATE_SEATS, (count, sub_id))
                    cur.execute(
                        _INSERT_OUTBOX,
                        (
                            uuid7(),
                            sub_id,
                            sub_id,
                            events.SEATS_CHANGED,
                            json.dumps({"workspace_id": workspace_id, "seats": count}),
                        ),
                    )
                    cur.execute(_NOTIFY)
                    changed += 1
            conn.commit()
    if changed:
        log.info("billing.seats.recalculated", workspaces_changed=changed)
    return changed


@celery_app.task(name="billing.sync_seats_to_stripe", queue="housekeeping")
def sync_seats_to_stripe() -> int:
    """Push only dirty subscriptions (``seats != seats_stripe_synced``) to Stripe."""
    settings = get_settings()
    dsn = settings.database_url_psycopg
    pushed = 0
    with psycopg.connect(dsn) as conn:
        conn.autocommit = False
        for workspace_id in _iter_workspace_ids(conn):
            with conn.cursor() as cur:
                cur.execute(_SET_GUC, (workspace_id,))
                cur.execute(_SELECT_SUBSCRIPTION, (workspace_id,))
                row = cur.fetchone()
            if row is None:
                conn.commit()
                continue
            sub_id, seats, seats_stripe_synced, item_id = row
            if item_id is None or seats == seats_stripe_synced:
                conn.commit()
                continue
            update_subscription_item_quantity_sync(
                settings=settings, subscription_item_id=item_id, quantity=seats
            )
            with conn.cursor() as cur:
                cur.execute(_SET_GUC, (workspace_id,))
                cur.execute(_UPDATE_SEATS_STRIPE_SYNCED, (seats, sub_id))
            conn.commit()
            pushed += 1
    if pushed:
        log.info("billing.seats.pushed_to_stripe", subscriptions_pushed=pushed)
    return pushed


# --- Neko resolution metering (P1.3, RFC-002 §5.6 async metering + reconciliation) ----------

_SELECT_STRIPE_CUSTOMER = "SELECT stripe_customer_id FROM subscriptions WHERE workspace_id = %s"
_SELECT_UNSYNCED_RESOLUTIONS = (
    "SELECT source_id, qty FROM usage_records "
    "WHERE meter = %s AND stripe_synced_at IS NULL ORDER BY occurred_at LIMIT %s"
)
# Guard on ``stripe_synced_at IS NULL`` so a concurrent pass can't double-mark, and on source_id
# (unique per (workspace, meter)) so we only touch the row we just reported.
_MARK_RESOLUTION_SYNCED = (
    "UPDATE usage_records SET stripe_synced_at = now() "
    "WHERE meter = %s AND source_id = %s AND stripe_synced_at IS NULL"
)
_SUM_RESOLUTIONS_PERIOD = (
    "SELECT coalesce(sum(qty), 0) FROM usage_records "
    "WHERE meter = %s AND occurred_at >= %s AND occurred_at < %s"
)
_RESOLUTION_SYNC_BATCH = 500


@celery_app.task(name="billing.sync_resolutions_to_stripe", queue="housekeeping")
def sync_resolutions_to_stripe() -> int:
    """Report un-synced Neko resolution meters to Stripe Billing Meters (P1.3).

    The async, off-request-path leg of the money loop (master rule 5): the request/worker txn only
    writes the ``usage_records`` row (via ``service.record_usage``); this poll reports it to Stripe
    and stamps ``stripe_synced_at``. Idempotent — the meter-event ``identifier`` is the row's
    ``source_id`` (Stripe dedupes), so a crash between the Stripe call and the local stamp just
    re-reports the same identifier next pass. Workspaces without a Stripe customer yet are skipped
    (their rows stay un-synced until they subscribe). Negative rows (claw-backs) report as
    negative-value events."""
    settings = get_settings()
    event_name = settings.stripe_resolution_meter_event
    dsn = settings.database_url_psycopg
    pushed = 0
    with psycopg.connect(dsn) as conn:
        conn.autocommit = False
        for workspace_id in _iter_workspace_ids(conn):
            with conn.cursor() as cur:
                cur.execute(_SET_GUC, (workspace_id,))
                cur.execute(_SELECT_STRIPE_CUSTOMER, (workspace_id,))
                sub_row = cur.fetchone()
                customer_id = sub_row[0] if sub_row else None
                if customer_id is None:
                    conn.commit()
                    continue
                cur.execute(
                    _SELECT_UNSYNCED_RESOLUTIONS, (RESOLUTION_METER, _RESOLUTION_SYNC_BATCH)
                )
                rows = cur.fetchall()
            for source_id, qty in rows:
                create_meter_event_sync(
                    settings=settings,
                    event_name=event_name,
                    stripe_customer_id=customer_id,
                    value=int(qty),
                    identifier=source_id,
                )
                with conn.cursor() as cur:
                    cur.execute(_SET_GUC, (workspace_id,))
                    cur.execute(_MARK_RESOLUTION_SYNCED, (RESOLUTION_METER, source_id))
                conn.commit()
                pushed += 1
    if pushed:
        log.info("billing.resolutions.pushed_to_stripe", meter_events=pushed)
    return pushed


def _prior_month_bounds(now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    this_month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    prior_month_start = (this_month_start - dt.timedelta(days=1)).replace(day=1)
    return prior_month_start, this_month_start


@celery_app.task(name="billing.reconcile_usage_monthly", queue="housekeeping")
def reconcile_usage_monthly() -> int:
    """Monthly reconciliation of Neko resolution meters (RFC-002 §5.6: Postgres is the source of
    truth, Stripe is synced asynchronously with reconciliation).

    For the just-closed month, per workspace: logs the authoritative net resolution total
    (``SUM(qty)``, claw-backs netted) as the billing record of truth, and re-reports any rows that
    the regular 5-min sync somehow left un-synced (belt-and-suspenders so a stalled sync can't drop
    a billable unit). Returns the number of workspaces with prior-month resolution activity.
    # ponytail: authoritative-total-vs-Stripe fetch-and-diff is the upgrade path — the Billing
    # Meter summary API lands here when we need drift alerting; the re-report already closes the
    # only lossy gap (a unit recorded locally but never pushed)."""
    settings = get_settings()
    dsn = settings.database_url_psycopg
    event_name = settings.stripe_resolution_meter_event
    start, end = _prior_month_bounds(dt.datetime.now(dt.UTC))
    reconciled = 0
    with psycopg.connect(dsn) as conn:
        conn.autocommit = False
        for workspace_id in _iter_workspace_ids(conn):
            with conn.cursor() as cur:
                cur.execute(_SET_GUC, (workspace_id,))
                cur.execute(_SUM_RESOLUTIONS_PERIOD, (RESOLUTION_METER, start, end))
                total_row = cur.fetchone()
                net_total = total_row[0] if total_row else 0
                cur.execute(_SELECT_STRIPE_CUSTOMER, (workspace_id,))
                sub_row = cur.fetchone()
                customer_id = sub_row[0] if sub_row else None
                cur.execute(
                    _SELECT_UNSYNCED_RESOLUTIONS, (RESOLUTION_METER, _RESOLUTION_SYNC_BATCH)
                )
                unsynced = cur.fetchall()
            if net_total:
                log.info(
                    "billing.resolutions.reconciled",
                    workspace_id=workspace_id,
                    month=start.date().isoformat(),
                    net_resolutions=str(net_total),
                    unsynced_rows=len(unsynced),
                )
                reconciled += 1
            if customer_id is not None:
                for source_id, qty in unsynced:
                    create_meter_event_sync(
                        settings=settings,
                        event_name=event_name,
                        stripe_customer_id=customer_id,
                        value=int(qty),
                        identifier=source_id,
                    )
                    with conn.cursor() as cur:
                        cur.execute(_SET_GUC, (workspace_id,))
                        cur.execute(_MARK_RESOLUTION_SYNCED, (RESOLUTION_METER, source_id))
                    conn.commit()
            conn.commit()
    return reconciled
