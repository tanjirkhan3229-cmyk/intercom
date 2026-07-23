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

import json

import psycopg

from relay.core.ids import uuid7
from relay.core.logging import get_logger
from relay.settings import get_settings
from relay.worker import celery_app

from . import events
from .stripe_client import update_subscription_item_quantity_sync

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
