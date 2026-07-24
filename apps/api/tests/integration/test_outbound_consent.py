"""P1.8 consent + subscription types + one-click unsubscribe (RFC 8058).

Proves: signup seeds default subscription types; admin consent set/list writes the projection +
audit trail; the one-click POST unsubscribes while GET is side-effect-free; forged/expired tokens
are inert; and the send-time gate honours marketing opt-out, transactional exemption, opt-in types.
"""

from __future__ import annotations

import uuid
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from relay.core.db import session_scope
from relay.core.ids import IdPrefix, decode_public_id
from relay.modules.outbound import service
from relay.modules.outbound.models import ConsentEvent, SubscriptionType
from relay.modules.outbound.unsubscribe_token import make_unsubscribe_token

pytestmark = pytest.mark.integration

PASSWORD = "password123"


async def _owner(client: httpx.AsyncClient, ws_name: str) -> tuple[str, uuid.UUID]:
    resp = await client.post(
        "/v0/auth/signup",
        json={
            "workspace_name": ws_name,
            "email": f"owner-{uuid4().hex}@example.com",
            "password": PASSWORD,
            "name": "Owner",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    return body["access_token"], decode_public_id(IdPrefix.WORKSPACE, body["workspace"]["id"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _subtype(
    client: httpx.AsyncClient,
    token: str,
    name: str,
    *,
    kind: str = "marketing",
    opt_in: bool = False,
) -> str:
    resp = await client.post(
        "/v0/outbound/subscription-types",
        json={"name": name, "kind": kind, "requires_opt_in": opt_in},
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _contact(client: httpx.AsyncClient, token: str, email: str) -> str:
    resp = await client.post("/v0/contacts/identify", json={"email": email}, headers=_auth(token))
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def test_signup_seeds_default_subscription_types(client: httpx.AsyncClient) -> None:
    token, _ws = await _owner(client, "Seeded")
    resp = await client.get("/v0/outbound/subscription-types", headers=_auth(token))
    assert resp.status_code == 200
    names = {t["name"] for t in resp.json()}
    assert {"Product updates", "Transactional"} <= names


async def test_admin_consent_set_and_audit_trail(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "ConsentWS")
    subtype = await _subtype(client, token, "Newsletter")
    contact = await _contact(client, token, "reader@example.com")

    # Set unsubscribed, then re-set subscribed → two audit rows, projection reflects the latest.
    for state in ("unsubscribed", "subscribed"):
        resp = await client.put(
            f"/v0/outbound/contacts/{contact}/consent",
            json={"subscription_type_id": subtype, "state": state},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["state"] == state

    listed = (
        await client.get(f"/v0/outbound/contacts/{contact}/consents", headers=_auth(token))
    ).json()
    assert len(listed) == 1 and listed[0]["state"] == "subscribed"

    async with session_scope(ws) as s:
        audit_rows = await s.scalar(select(func.count()).select_from(ConsentEvent))
    assert audit_rows == 2  # append-only: one row per change


async def test_one_click_unsubscribe(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "UnsubWS")
    subtype = await _subtype(client, token, "Promotions")
    contact = await _contact(client, token, "buyer@example.com")
    contact_uuid = decode_public_id(IdPrefix.CONTACT, contact)
    subtype_uuid = decode_public_id(IdPrefix.SUBSCRIPTION_TYPE, subtype)
    unsub = make_unsubscribe_token(ws, contact_uuid, subtype_uuid)

    # GET renders the confirmation page and must NOT change state (scanners prefetch GET).
    page = await client.get(f"/v0/outbound/u/{unsub}")
    assert page.status_code == 200 and "Promotions" in page.text
    async with session_scope(ws) as s:
        assert await s.scalar(select(func.count()).select_from(ConsentEvent)) == 0

    # POST one-click unsubscribes.
    done = await client.post(f"/v0/outbound/u/{unsub}", data={"List-Unsubscribe": "One-Click"})
    assert done.status_code == 200 and "unsubscribed" in done.text.lower()
    assert await service_state(ws, contact_uuid, subtype_uuid) == "unsubscribed"

    async with session_scope(ws) as s:
        row = (await s.execute(select(ConsentEvent))).scalars().one()
    assert row.to_state == "unsubscribed" and row.source == "list_unsubscribe"


async def test_forged_and_expired_tokens_are_inert(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "ForgeWS")
    subtype = await _subtype(client, token, "Digest")
    contact = await _contact(client, token, "x@example.com")
    contact_uuid = decode_public_id(IdPrefix.CONTACT, contact)
    subtype_uuid = decode_public_id(IdPrefix.SUBSCRIPTION_TYPE, subtype)

    good = make_unsubscribe_token(ws, contact_uuid, subtype_uuid)
    forged = good[:-4] + ("AAAA" if good[-4:] != "AAAA" else "BBBB")
    expired = make_unsubscribe_token(ws, contact_uuid, subtype_uuid, ttl_seconds=-10)

    for bad in (forged, expired, "not-a-token"):
        page = await client.get(f"/v0/outbound/u/{bad}")
        assert page.status_code == 200 and "invalid or has expired" in page.text
        post = await client.post(f"/v0/outbound/u/{bad}", data={"List-Unsubscribe": "One-Click"})
        assert post.status_code == 200

    # No consent was ever written by the inert requests.
    async with session_scope(ws) as s:
        assert await s.scalar(select(func.count()).select_from(ConsentEvent)) == 0


async def test_send_time_consent_gate(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "GateWS")
    marketing = decode_public_id(IdPrefix.SUBSCRIPTION_TYPE, await _subtype(client, token, "Mkt"))
    transactional = decode_public_id(
        IdPrefix.SUBSCRIPTION_TYPE, await _subtype(client, token, "Txn", kind="transactional")
    )
    optin = decode_public_id(
        IdPrefix.SUBSCRIPTION_TYPE, await _subtype(client, token, "OptIn", opt_in=True)
    )
    contact = decode_public_id(IdPrefix.CONTACT, await _contact(client, token, "g@example.com"))

    async with session_scope(ws) as s:
        mkt = await s.get(SubscriptionType, marketing)
        txn = await s.get(SubscriptionType, transactional)
        opt = await s.get(SubscriptionType, optin)
        assert mkt and txn and opt
        # Marketing (opt-out default): not blocked until an explicit unsubscribe.
        assert (
            await service.is_blocked_by_consent(s, contact_id=contact, subscription_type=mkt)
            is False
        )
        # Transactional: never blocked, even with no consent row.
        assert (
            await service.is_blocked_by_consent(s, contact_id=contact, subscription_type=txn)
            is False
        )
        # Opt-in: blocked until an explicit subscribe.
        assert (
            await service.is_blocked_by_consent(s, contact_id=contact, subscription_type=opt)
            is True
        )

    async with session_scope(ws) as s:
        await service.set_consent(
            s,
            workspace_id=ws,
            contact_id=contact,
            subscription_type_id=marketing,
            state="unsubscribed",
            source="api",
            actor_kind="admin",
        )
        await service.set_consent(
            s,
            workspace_id=ws,
            contact_id=contact,
            subscription_type_id=optin,
            state="subscribed",
            source="api",
            actor_kind="admin",
        )

    async with session_scope(ws) as s:
        mkt = await s.get(SubscriptionType, marketing)
        opt = await s.get(SubscriptionType, optin)
        assert mkt and opt
        assert (
            await service.is_blocked_by_consent(s, contact_id=contact, subscription_type=mkt)
            is True
        )
        assert (
            await service.is_blocked_by_consent(s, contact_id=contact, subscription_type=opt)
            is False
        )


async def service_state(
    ws: uuid.UUID, contact_id: uuid.UUID, subscription_type_id: uuid.UUID
) -> str | None:
    async with session_scope(ws) as s:
        return await service.get_consent_state(s, contact_id, subscription_type_id)
