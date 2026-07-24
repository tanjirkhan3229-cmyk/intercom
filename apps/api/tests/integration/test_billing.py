"""Billing integration tests (P0.10 acceptance, RFC-002 §5.6).

Stripe itself is never called — ``billing.service._get_stripe_client`` is monkeypatched to a
fake that returns canned URLs, so the checkout/portal flow is exercised end to end without
network. The trial -> subscribe -> seat add -> payment fail -> recovery lifecycle is driven
by posting hand-signed webhook payloads (the same HMAC scheme Stripe itself uses), proving
signature verification, event dispatch, and idempotent-by-event-id processing all work
against the real Postgres/RLS stack.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from relay.core.db import session_scope
from relay.core.ids import IdPrefix, decode_public_id
from relay.modules.billing import service as billing_service
from relay.modules.billing.models import Plan, StripeWebhookEvent, Subscription, UsageRecord
from relay.settings import get_settings

pytestmark = pytest.mark.integration

PASSWORD = "password123"


class FakeStripeClient:
    """Stands in for ``StripeClient`` — no network, canned responses."""

    async def create_checkout_session(self, **_kwargs: object) -> dict:
        return {"id": "cs_test_1", "url": "https://checkout.stripe.com/test-session"}

    async def create_portal_session(self, **_kwargs: object) -> dict:
        return {"id": "bps_test_1", "url": "https://billing.stripe.com/test-portal"}

    async def update_subscription_item_quantity(self, **kwargs: object) -> dict:
        return {"id": kwargs["subscription_item_id"], "quantity": kwargs["quantity"]}


@pytest.fixture(autouse=True)
def _fake_stripe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(billing_service, "_get_stripe_client", lambda: FakeStripeClient())


async def _owner(client: httpx.AsyncClient, ws_name: str) -> tuple[str, str]:
    resp = await client.post(
        "/v0/auth/signup",
        json={
            "workspace_name": ws_name,
            "email": f"owner-{uuid4().hex}@example.com",
            "password": PASSWORD,
            "name": "Owner",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["access_token"], body["workspace"]["id"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _sign(payload: bytes, secret: str) -> str:
    ts = int(time.time())
    signed_payload = f"{ts}.".encode() + payload
    sig = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


async def _send_webhook(
    client: httpx.AsyncClient, event_type: str, obj: dict, *, event_id: str | None = None
) -> httpx.Response:
    event = {"id": event_id or f"evt_{uuid4().hex}", "type": event_type, "data": {"object": obj}}
    payload = json.dumps(event).encode()
    header = _sign(payload, get_settings().stripe_webhook_secret)
    return await client.post(
        "/v0/billing/webhook",
        content=payload,
        headers={"Stripe-Signature": header, "Content-Type": "application/json"},
    )


async def _subscription_created_payload(
    workspace_public_id: str, plan: Plan, *, status: str = "trialing"
) -> tuple[dict, str, str]:
    stripe_subscription_id = f"sub_{uuid4().hex}"
    item_id = f"si_{uuid4().hex}"
    now = int(time.time())
    obj = {
        "id": stripe_subscription_id,
        "customer": f"cus_{uuid4().hex}",
        "status": status,
        "trial_end": now + 14 * 86400,
        "current_period_end": now + 30 * 86400,
        "items": {"data": [{"id": item_id, "quantity": 1, "price": {"id": plan.stripe_price_id}}]},
        "metadata": {"workspace_id": workspace_public_id},
    }
    return obj, stripe_subscription_id, item_id


# --- Checkout / portal (no Stripe call inside a request-path transaction) ------------------


async def test_checkout_session_returns_stripe_url(client: httpx.AsyncClient) -> None:
    tok, _ws = await _owner(client, "Acme")
    resp = await client.post(
        "/v0/billing/checkout-session", json={"plan_code": "starter"}, headers=_auth(tok)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["url"] == "https://checkout.stripe.com/test-session"


async def test_checkout_session_unknown_plan_404s(client: httpx.AsyncClient) -> None:
    tok, _ws = await _owner(client, "Bravo")
    resp = await client.post(
        "/v0/billing/checkout-session", json={"plan_code": "nonexistent"}, headers=_auth(tok)
    )
    assert resp.status_code == 404


async def test_portal_session_requires_existing_customer(client: httpx.AsyncClient) -> None:
    tok, _ws = await _owner(client, "Charlie")
    resp = await client.post("/v0/billing/portal-session", headers=_auth(tok))
    assert resp.status_code == 404  # no subscription/customer yet


# --- Full lifecycle: trial -> subscribe -> seat add -> payment fail -> recovery ------------


async def test_subscription_lifecycle_via_webhooks(client: httpx.AsyncClient) -> None:
    tok, ws_pub = await _owner(client, "Delta")
    ws_id = decode_public_id(IdPrefix.WORKSPACE, ws_pub)

    async with session_scope(ws_id) as session:
        plan = await session.scalar(select(Plan).where(Plan.code == "starter"))
    assert plan is not None

    obj, stripe_sub_id, _item_id = await _subscription_created_payload(ws_pub, plan)
    resp = await _send_webhook(client, "customer.subscription.created", obj)
    assert resp.status_code == 204

    resp = await client.get("/v0/billing/subscription", headers=_auth(tok))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "trialing"
    assert body["plan_code"] == "starter"
    assert body["seats"] == 1  # just the owner so far
    assert body["banner_state"] == "none"

    # --- seat add: inviting a member recalculates seats in the same transaction ---
    resp = await client.post(
        "/v0/members",
        json={"email": f"agent-{uuid4().hex}@example.com", "name": "Agent", "role": "agent"},
        headers=_auth(tok),
    )
    assert resp.status_code == 201, resp.text

    async with session_scope(ws_id) as session:
        sub = await session.scalar(select(Subscription).where(Subscription.workspace_id == ws_id))
    assert sub is not None
    assert sub.seats == 2

    # --- subscribe: Stripe confirms active ---
    active_obj = {**obj, "status": "active"}
    resp = await _send_webhook(client, "customer.subscription.updated", active_obj)
    assert resp.status_code == 204
    body = (await client.get("/v0/billing/subscription", headers=_auth(tok))).json()
    assert body["status"] == "active"

    # --- payment fail: dunning banner state ---
    invoice_failed = {"subscription": stripe_sub_id, "id": f"in_{uuid4().hex}"}
    resp = await _send_webhook(client, "invoice.payment_failed", invoice_failed)
    assert resp.status_code == 204
    body = (await client.get("/v0/billing/subscription", headers=_auth(tok))).json()
    assert body["status"] == "past_due"
    assert body["banner_state"] == "payment_failed"

    # --- recovery: payment succeeds, banner clears ---
    invoice_succeeded = {"subscription": stripe_sub_id, "id": f"in_{uuid4().hex}"}
    resp = await _send_webhook(client, "invoice.payment_succeeded", invoice_succeeded)
    assert resp.status_code == 204
    body = (await client.get("/v0/billing/subscription", headers=_auth(tok))).json()
    assert body["status"] == "active"
    assert body["banner_state"] == "none"

    # Portal session now succeeds — a Stripe customer id was set by subscription.created.
    resp = await client.post("/v0/billing/portal-session", headers=_auth(tok))
    assert resp.status_code == 200
    assert resp.json()["url"] == "https://billing.stripe.com/test-portal"


# --- Webhook idempotency (acceptance: usage_records/webhooks survive a duplicate) ----------


async def test_webhook_is_idempotent_by_event_id(client: httpx.AsyncClient) -> None:
    _tok, ws_pub = await _owner(client, "Echo")
    ws_id = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    async with session_scope(ws_id) as session:
        plan = await session.scalar(select(Plan).where(Plan.code == "starter"))
    assert plan is not None

    obj, _stripe_sub_id, _item_id = await _subscription_created_payload(ws_pub, plan)
    event_id = f"evt_{uuid4().hex}"

    resp1 = await _send_webhook(client, "customer.subscription.created", obj, event_id=event_id)
    assert resp1.status_code == 204
    resp2 = await _send_webhook(client, "customer.subscription.created", obj, event_id=event_id)
    assert resp2.status_code == 204

    async with session_scope(ws_id) as session:
        sub_count = await session.scalar(
            select(func.count()).select_from(Subscription).where(Subscription.workspace_id == ws_id)
        )
    assert sub_count == 1  # duplicate delivery created nothing extra

    async with session_scope() as session:
        event_count = await session.scalar(
            select(func.count())
            .select_from(StripeWebhookEvent)
            .where(StripeWebhookEvent.id == event_id)
        )
    assert event_count == 1


async def test_webhook_rejects_bad_signature(client: httpx.AsyncClient) -> None:
    payload = json.dumps({"id": "evt_bad", "type": "customer.subscription.created"}).encode()
    resp = await client.post(
        "/v0/billing/webhook",
        content=payload,
        headers={"Stripe-Signature": "t=1,v1=deadbeef", "Content-Type": "application/json"},
    )
    assert resp.status_code == 422  # ValidationError (relay.core.errors) maps to 422


# --- Cross-tenant isolation (master rule 1) -------------------------------------------------


async def test_cross_tenant_isolation_subscriptions_and_usage(client: httpx.AsyncClient) -> None:
    tok_a, ws_a_pub = await _owner(client, "Foxtrot")
    tok_b, _ws_b_pub = await _owner(client, "Golf")
    ws_a = decode_public_id(IdPrefix.WORKSPACE, ws_a_pub)

    async with session_scope(ws_a) as session:
        plan = await session.scalar(select(Plan).where(Plan.code == "starter"))
    assert plan is not None
    obj, _sid, _iid = await _subscription_created_payload(ws_a_pub, plan)
    resp = await _send_webhook(client, "customer.subscription.created", obj)
    assert resp.status_code == 204

    # B has no subscription of its own — must not see A's.
    resp_b = await client.get("/v0/billing/subscription", headers=_auth(tok_b))
    assert resp_b.status_code == 404
    resp_a = await client.get("/v0/billing/subscription", headers=_auth(tok_a))
    assert resp_a.status_code == 200

    # Unset app.ws entirely: tenant tables return zero rows even though data exists.
    from relay.core.db import get_sessionmaker

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        count = await session.scalar(select(func.count()).select_from(Subscription))
    assert count == 0


# --- Generic meter interface (RFC-002 §5.6 W8) ----------------------------------------------


async def test_record_usage_is_idempotent_by_source_id(client: httpx.AsyncClient) -> None:
    """The generic meter: same (meter, source_id) records exactly once (P1.3 Aide plugs in
    here); a negative correction is a distinct append-only row."""
    _tok, ws_pub = await _owner(client, "Hotel")
    ws_id = decode_public_id(IdPrefix.WORKSPACE, ws_pub)

    async with session_scope(ws_id) as session:
        first = await billing_service.record_usage(
            session, workspace_id=ws_id, meter="aide.resolution", qty=1, source_id="cnv_1"
        )
    # Redelivery of the same triggering event — must be a no-op.
    async with session_scope(ws_id) as session:
        second = await billing_service.record_usage(
            session, workspace_id=ws_id, meter="aide.resolution", qty=1, source_id="cnv_1"
        )
    # A correction is a NEW row (its own source_id), negative qty — append-only.
    async with session_scope(ws_id) as session:
        corrected = await billing_service.record_usage(
            session,
            workspace_id=ws_id,
            meter="aide.resolution",
            qty=-1,
            source_id="cnv_1:correction",
        )

    assert first is True
    assert second is False  # duplicate source_id — replay-safe no-op
    assert corrected is True

    async with session_scope(ws_id) as session:
        count = await session.scalar(
            select(func.count()).select_from(UsageRecord).where(UsageRecord.workspace_id == ws_id)
        )
    assert count == 2  # the original + the correction; the duplicate inserted nothing


async def test_usage_records_are_cross_tenant_isolated(client: httpx.AsyncClient) -> None:
    _tok_a, ws_a_pub = await _owner(client, "India")
    _tok_b, ws_b_pub = await _owner(client, "Juliet")
    ws_a = decode_public_id(IdPrefix.WORKSPACE, ws_a_pub)
    ws_b = decode_public_id(IdPrefix.WORKSPACE, ws_b_pub)

    async with session_scope(ws_a) as session:
        await billing_service.record_usage(
            session, workspace_id=ws_a, meter="aide.resolution", qty=1, source_id="cnv_a"
        )

    # B's RLS-scoped view must not include A's usage rows.
    async with session_scope(ws_b) as session:
        b_count = await session.scalar(select(func.count()).select_from(UsageRecord))
    assert b_count == 0
