"""Service layer for the ``billing`` module — the cross-module interface (RFC-002 §5.6, P0.10).

Master rule 5 / acceptance criterion: **no Stripe call happens inside a request-path DB
transaction.** ``create_checkout_session``/``create_portal_session`` open a short read-only
session (via ``session_scope``), close it, *then* call Stripe. Seat pushes to Stripe never
happen on the request path at all — ``recalculate_seats`` only ever updates the local
``seats`` column and lets ``tasks.sync_seats_to_stripe`` (a Celery task, not a request) push
the delta.

Webhook processing (``handle_webhook_event``) is the exception: it is inbound, not
outbound, so verifying + applying it inside one transaction is correct and gives us the
idempotent-by-event-id guarantee via ``stripe_webhook_events`` (dedupe insert and the state
update commit together — a duplicate delivery is a same-transaction no-op).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from relay.core import outbox
from relay.core.db import session_scope, set_workspace_guc
from relay.core.errors import NotFoundError, ValidationError
from relay.core.ids import IdPrefix, decode_public_id, encode_public_id, uuid7
from relay.core.principal import Principal
from relay.core.rbac import Role, authorize
from relay.modules.identity import service as identity_service
from relay.settings import get_settings

from . import events, schemas
from .models import Plan, StripeWebhookEvent, Subscription, UsageRecord
from .stripe_client import StripeClient, verify_and_parse_event

# Statuses that count as "in good standing" for entitlements (RFC-000 §8: seats now).
ACTIVE_STATUSES: frozenset[str] = frozenset({"trialing", "active"})


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _get_stripe_client() -> StripeClient:
    """Factory seam — tests monkeypatch this to inject a fake client (no live network)."""
    return StripeClient(get_settings())


# --- Plans ----------------------------------------------------------------------


async def _get_plan_by_code(session: AsyncSession, code: str) -> Plan:
    plan = await session.scalar(select(Plan).where(Plan.code == code, Plan.is_active.is_(True)))
    if plan is None:
        raise NotFoundError(f"unknown plan '{code}'")
    return plan


async def _get_plan_by_price_id(session: AsyncSession, price_id: str) -> Plan | None:
    plan: Plan | None = await session.scalar(select(Plan).where(Plan.stripe_price_id == price_id))
    return plan


# --- Checkout / portal (Stripe calls happen outside any open DB transaction) ----


async def create_checkout_session(
    principal: Principal, req: schemas.CheckoutSessionCreate
) -> schemas.CheckoutSessionOut:
    authorize(principal, min_role=Role.OWNER)
    settings = get_settings()
    async with session_scope(principal.workspace_id) as session:
        plan = await _get_plan_by_code(session, req.plan_code)
        email = await identity_service.get_admin_email(session, principal.admin_id)

    result = await _get_stripe_client().create_checkout_session(
        price_id=plan.stripe_price_id,
        customer_email=email,
        workspace_id=encode_public_id(IdPrefix.WORKSPACE, principal.workspace_id),
        trial_days=plan.trial_days,
        success_url=settings.stripe_checkout_success_url,
        cancel_url=settings.stripe_checkout_cancel_url,
    )
    return schemas.CheckoutSessionOut(url=result["url"])


async def create_portal_session(principal: Principal) -> schemas.PortalSessionOut:
    authorize(principal, min_role=Role.OWNER)
    settings = get_settings()
    async with session_scope(principal.workspace_id) as session:
        sub = await session.scalar(
            select(Subscription).where(Subscription.workspace_id == principal.workspace_id)
        )
        if sub is None or sub.stripe_customer_id is None:
            raise NotFoundError("no billing customer for this workspace yet")
        customer_id = sub.stripe_customer_id

    result = await _get_stripe_client().create_portal_session(
        customer_id=customer_id, return_url=settings.stripe_portal_return_url
    )
    return schemas.PortalSessionOut(url=result["url"])


async def get_billing_summary(
    session: AsyncSession, principal: Principal
) -> schemas.SubscriptionOut:
    sub = await session.scalar(
        select(Subscription).where(Subscription.workspace_id == principal.workspace_id)
    )
    if sub is None:
        raise NotFoundError("no subscription for this workspace yet")
    plan = await session.get(Plan, sub.plan_id)
    assert plan is not None
    return schemas.SubscriptionOut(
        id=encode_public_id(IdPrefix.SUBSCRIPTION, sub.id),
        plan_code=plan.code,
        status=sub.status,
        banner_state=sub.banner_state,
        seats=sub.seats,
        trial_ends_at=sub.trial_ends_at,
        current_period_end=sub.current_period_end,
    )


# --- Entitlements (consulted by feature gates; RFC-000 §8) ----------------------


@dataclass(frozen=True)
class Entitlements:
    has_subscription: bool
    plan_code: str | None
    status: str | None
    is_active: bool  # trialing or active — the feature-gate boolean


async def get_entitlements(session: AsyncSession, workspace_id: uuid.UUID) -> Entitlements:
    sub = await session.scalar(
        select(Subscription).where(Subscription.workspace_id == workspace_id)
    )
    if sub is None:
        return Entitlements(has_subscription=False, plan_code=None, status=None, is_active=False)
    plan = await session.get(Plan, sub.plan_id)
    assert plan is not None
    return Entitlements(
        has_subscription=True,
        plan_code=plan.code,
        status=sub.status,
        is_active=sub.status in ACTIVE_STATUSES,
    )


# --- Seats (RFC-002 §5.6: seat counting synced to Stripe quantity daily + on change) --------


async def recalculate_seats(session: AsyncSession, workspace_id: uuid.UUID) -> None:
    """Recompute the local seat count from active memberships.

    Only ever writes the local ``seats`` column + an outbox event — never calls Stripe. The
    actual Stripe push is a Celery task (``tasks.sync_seats_to_stripe``) that polls for rows
    where ``seats != seats_stripe_synced`` (RFC-001 §5: no external call inside a
    request-path transaction). Called both on membership add/remove (near-real-time) and by
    the daily reconciliation task (``tasks.recalculate_all_seats``).
    """
    sub = await session.scalar(
        select(Subscription).where(Subscription.workspace_id == workspace_id)
    )
    if sub is None:
        return  # no subscription yet — nothing to keep in sync
    count = await identity_service.count_active_memberships(session, workspace_id)
    if count == sub.seats:
        return
    sub.seats = count
    await session.flush()
    await outbox.emit(
        session,
        aggregate="subscription",
        aggregate_id=sub.id,
        topic=events.SEATS_CHANGED,
        payload={"workspace_id": str(workspace_id), "seats": count},
    )


# --- Usage metering (RFC-002 §5.6 W8: the generic meter interface) --------------------------


async def record_usage(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    meter: str,
    qty: int | float,
    source_id: str,
    occurred_at: dt.datetime | None = None,
) -> bool:
    """Append a usage row idempotently — the generic meter interface (RFC-002 §5.6 W8).

    Any module calls this in the **same transaction** as its triggering domain write (e.g.
    P1.3 Aide resolutions): billing never learns what a "resolution" is, it just counts
    ``(meter, qty)`` keyed by ``source_id``. Idempotent by
    ``UNIQUE(workspace_id, meter, source_id)`` — a redelivered domain event or retried task
    re-calls with the same ``source_id`` and inserts nothing the second time. Corrections are
    a NEW row with a negative ``qty`` and its own ``source_id`` (the table is append-only).

    Returns True if a row was inserted, False if this ``source_id`` was already recorded.
    Emits a ``usage.recorded`` outbox row on first insert only (master rule 2: billing meters
    ride the consistency spine), so async Stripe metering fires exactly once per unit.
    """
    stmt = (
        pg_insert(UsageRecord)
        .values(
            id=uuid7(),
            workspace_id=workspace_id,
            meter=meter,
            qty=qty,
            source_id=source_id,
            occurred_at=occurred_at or _now(),
        )
        .on_conflict_do_nothing(
            index_elements=[UsageRecord.workspace_id, UsageRecord.meter, UsageRecord.source_id]
        )
        .returning(UsageRecord.id)
    )
    inserted = (await session.execute(stmt)).scalar_one_or_none()
    if inserted is None:
        return False  # duplicate source_id — replay-safe no-op
    await outbox.emit(
        session,
        aggregate="usage_record",
        aggregate_id=inserted,
        topic=events.USAGE_RECORDED,
        payload={
            "workspace_id": str(workspace_id),
            "meter": meter,
            "qty": qty,
            "source_id": source_id,
        },
    )
    return True


# --- Webhooks (RFC-002 §5.6: idempotent by event id, dunning -> banner state) ---------------


def _decode_workspace_metadata(obj: dict[str, Any]) -> uuid.UUID | None:
    public_id = (obj.get("metadata") or {}).get("workspace_id") or obj.get("client_reference_id")
    if not public_id:
        return None
    try:
        return decode_public_id(IdPrefix.WORKSPACE, public_id)
    except ValueError:
        return None


async def _resolve_workspace_by_subscription(
    session: AsyncSession, stripe_subscription_id: str
) -> uuid.UUID | None:
    """SECURITY DEFINER lookup (mirrors identity's ``identity_admin_workspaces``): resolves
    the owning workspace before any RLS GUC is set, for webhook event types whose payload
    carries the subscription id but not our workspace metadata (e.g. invoices)."""
    row = await session.execute(
        sa.select(sa.func.billing_workspace_by_stripe_subscription(stripe_subscription_id))
    )
    value = row.scalar_one_or_none()
    return uuid.UUID(str(value)) if value else None


async def _apply_subscription_created(
    session: AsyncSession, workspace_id: uuid.UUID, obj: dict[str, Any]
) -> None:
    items = obj.get("items", {}).get("data", [])
    price_id = items[0]["price"]["id"] if items else None
    plan = await _get_plan_by_price_id(session, price_id) if price_id else None
    if plan is None:
        return
    seats = await identity_service.count_active_memberships(session, workspace_id)
    sub_id = uuid7()
    stmt = (
        pg_insert(Subscription)
        .values(
            id=sub_id,
            workspace_id=workspace_id,
            plan_id=plan.id,
            stripe_customer_id=obj.get("customer"),
            stripe_subscription_id=obj.get("id"),
            stripe_subscription_item_id=items[0]["id"] if items else None,
            status=obj.get("status", "trialing"),
            seats=seats,
            seats_stripe_synced=items[0].get("quantity") if items else None,
            trial_ends_at=_ts(obj.get("trial_end")),
            current_period_end=_ts(obj.get("current_period_end")),
        )
        .on_conflict_do_update(
            index_elements=[Subscription.workspace_id],
            set_={
                "stripe_customer_id": obj.get("customer"),
                "stripe_subscription_id": obj.get("id"),
                "stripe_subscription_item_id": items[0]["id"] if items else None,
                "status": obj.get("status", "trialing"),
                "trial_ends_at": _ts(obj.get("trial_end")),
                "current_period_end": _ts(obj.get("current_period_end")),
            },
        )
        .returning(Subscription.id)
    )
    result_id = (await session.execute(stmt)).scalar_one()
    await outbox.emit(
        session,
        aggregate="subscription",
        aggregate_id=result_id,
        topic=events.SUBSCRIPTION_CREATED,
        payload={"workspace_id": str(workspace_id), "status": obj.get("status")},
    )


def _ts(epoch: int | None) -> dt.datetime | None:
    return dt.datetime.fromtimestamp(epoch, tz=dt.UTC) if epoch is not None else None


async def _apply_subscription_updated(
    session: AsyncSession, workspace_id: uuid.UUID, obj: dict[str, Any]
) -> None:
    sub = await session.scalar(
        select(Subscription).where(Subscription.workspace_id == workspace_id)
    )
    if sub is None:
        return
    was_past_due = sub.status == "past_due"
    sub.status = obj.get("status", sub.status)
    sub.current_period_end = _ts(obj.get("current_period_end")) or sub.current_period_end
    sub.trial_ends_at = _ts(obj.get("trial_end")) or sub.trial_ends_at
    if sub.status in ACTIVE_STATUSES:
        sub.banner_state = "none"
    await session.flush()
    topic = (
        events.PAYMENT_RECOVERED
        if was_past_due and sub.status == "active"
        else events.SUBSCRIPTION_UPDATED
    )
    await outbox.emit(
        session,
        aggregate="subscription",
        aggregate_id=sub.id,
        topic=topic,
        payload={"workspace_id": str(workspace_id), "status": sub.status},
    )


async def _apply_subscription_deleted(
    session: AsyncSession, workspace_id: uuid.UUID, obj: dict[str, Any]
) -> None:
    sub = await session.scalar(
        select(Subscription).where(Subscription.workspace_id == workspace_id)
    )
    if sub is None:
        return
    sub.status = "canceled"
    sub.banner_state = "canceled"
    sub.canceled_at = _now()
    await session.flush()
    await outbox.emit(
        session,
        aggregate="subscription",
        aggregate_id=sub.id,
        topic=events.SUBSCRIPTION_CANCELED,
        payload={"workspace_id": str(workspace_id)},
    )


async def _apply_trial_will_end(
    session: AsyncSession, workspace_id: uuid.UUID, obj: dict[str, Any]
) -> None:
    sub = await session.scalar(
        select(Subscription).where(Subscription.workspace_id == workspace_id)
    )
    if sub is None:
        return
    sub.banner_state = "trial_ending"
    await session.flush()
    await outbox.emit(
        session,
        aggregate="subscription",
        aggregate_id=sub.id,
        topic=events.SUBSCRIPTION_TRIAL_ENDING,
        payload={"workspace_id": str(workspace_id)},
    )


async def _apply_invoice_payment_failed(
    session: AsyncSession, workspace_id: uuid.UUID, obj: dict[str, Any]
) -> None:
    sub = await session.scalar(
        select(Subscription).where(Subscription.workspace_id == workspace_id)
    )
    if sub is None:
        return
    sub.status = "past_due"
    sub.banner_state = "payment_failed"
    await session.flush()
    await outbox.emit(
        session,
        aggregate="subscription",
        aggregate_id=sub.id,
        topic=events.PAYMENT_FAILED,
        payload={"workspace_id": str(workspace_id)},
    )


async def _apply_invoice_payment_succeeded(
    session: AsyncSession, workspace_id: uuid.UUID, obj: dict[str, Any]
) -> None:
    sub = await session.scalar(
        select(Subscription).where(Subscription.workspace_id == workspace_id)
    )
    if sub is None or sub.banner_state != "payment_failed":
        return
    sub.status = "active"
    sub.banner_state = "none"
    await session.flush()
    await outbox.emit(
        session,
        aggregate="subscription",
        aggregate_id=sub.id,
        topic=events.PAYMENT_RECOVERED,
        payload={"workspace_id": str(workspace_id)},
    )


_HANDLERS = {
    "customer.subscription.created": _apply_subscription_created,
    "customer.subscription.updated": _apply_subscription_updated,
    "customer.subscription.deleted": _apply_subscription_deleted,
    "customer.subscription.trial_will_end": _apply_trial_will_end,
    "invoice.payment_failed": _apply_invoice_payment_failed,
    "invoice.payment_succeeded": _apply_invoice_payment_succeeded,
}


async def handle_webhook_event(*, payload: bytes, sig_header: str | None) -> None:
    """Verify + dispatch a Stripe webhook. Idempotent by Stripe's event id.

    The dedupe insert and the domain update commit in the **same transaction**: a duplicate
    delivery either loses the ``ON CONFLICT`` race (no rows touched, transaction is a no-op)
    or, if the first delivery never committed, is free to be reprocessed — no lost or
    double-applied events (RFC-002 §7 pattern, mirrors ``core.idempotency``).

    The owning workspace is resolved *before* the RLS GUC is set (metadata on the event, or —
    for invoice events, which carry no metadata — the ``billing_workspace_by_stripe_subscription``
    SECURITY DEFINER lookup, mirroring identity's pre-GUC ``identity_admin_workspaces``), then
    ``set_workspace_guc`` is applied once so every handler's tenant-table query is RLS-scoped.
    """
    settings = get_settings()
    event = verify_and_parse_event(
        payload=payload, sig_header=sig_header, webhook_secret=settings.stripe_webhook_secret
    )
    event_id = event.get("id")
    event_type = event.get("type")
    if not event_id or not event_type:
        raise ValidationError("malformed Stripe event")

    handler = _HANDLERS.get(event_type)
    obj = event.get("data", {}).get("object", {})

    async with session_scope() as session:
        claim = (
            pg_insert(StripeWebhookEvent)
            .values(id=event_id, type=event_type)
            .on_conflict_do_nothing(index_elements=[StripeWebhookEvent.id])
            .returning(StripeWebhookEvent.id)
        )
        claimed = (await session.execute(claim)).scalar_one_or_none()
        if claimed is None:
            return  # already processed — replay-safe no-op
        if handler is None:
            return

        workspace_id = _decode_workspace_metadata(obj)
        if workspace_id is None:
            stripe_subscription_id = obj.get("subscription") or obj.get("id")
            if stripe_subscription_id:
                workspace_id = await _resolve_workspace_by_subscription(
                    session, stripe_subscription_id
                )
        if workspace_id is None:
            return  # not resolvable to one of our workspaces — ignore rather than fail

        await set_workspace_guc(session, workspace_id)
        await handler(session, workspace_id, obj)
