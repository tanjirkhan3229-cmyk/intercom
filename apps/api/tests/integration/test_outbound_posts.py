"""P1.8 in-app posts & chats — snapshot → per-contact delivery, catch-up, seen, chat, consent.

Drives the post pipeline in-process: fire → snapshot (pending receipts) → deliver → outbox
``outbound.post.delivered`` (fanned out to the contact channel) + widget-boot catch-up. Also
proves a ``chat`` post opens a conversation, consent is gated at delivery, delivery is exactly-once.
"""

from __future__ import annotations

import asyncio
import uuid
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from relay.core.db import session_scope
from relay.core.ids import IdPrefix, decode_public_id
from relay.modules.messaging.models import Conversation
from relay.modules.outbound import service
from relay.modules.outbound.models import PostReceipt

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


async def _contact(client: httpx.AsyncClient, token: str) -> uuid.UUID:
    resp = await client.post(
        "/v0/contacts/identify",
        json={"email": f"c-{uuid4().hex}@example.com"},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    return decode_public_id(IdPrefix.CONTACT, resp.json()["id"])


async def _post(
    client: httpx.AsyncClient, token: str, *, kind: str = "post", subtype_id: str | None = None
) -> str:
    resp = await client.post(
        "/v0/outbound/posts",
        json={
            "kind": kind,
            "title": "Big news",
            "body": {"text": "We shipped it!"},
            "subscription_type_id": subtype_id,
            "segment": {},
        },
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _fire_and_deliver(
    client: httpx.AsyncClient, token: str, ws: uuid.UUID, post_pub: str
) -> list[str]:
    """Fire, snapshot, and deliver all receipts; return per-contact delivery results."""
    fired = await client.post(f"/v0/outbound/posts/{post_pub}/fire", headers=_auth(token))
    assert fired.status_code == 200, fired.text
    post_id = decode_public_id(IdPrefix.POST, post_pub)
    contacts: list[uuid.UUID] = []
    await service.run_post_snapshot(ws, post_id, enqueue=contacts.extend)
    return [
        await service.deliver_post_receipt(workspace_id=ws, post_id=post_id, contact_id=c)
        for c in contacts
    ]


async def test_post_delivery_catchup_and_seen(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "Posts")
    contact_id = await _contact(client, token)
    post_pub = await _post(client, token)

    results = await _fire_and_deliver(client, token, ws, post_pub)
    assert results == ["delivered"]

    # Widget-boot catch-up: the delivered post is pending (unseen) for the contact.
    async with session_scope(ws) as s:
        pending = await service.pending_posts_for_contact(s, contact_id)
    assert len(pending) == 1 and pending[0]["title"] == "Big news"
    receipt_pub = pending[0]["receipt_id"]

    # Mark seen → no longer surfaced on the next boot.
    async with session_scope(ws) as s:
        await service.mark_post_seen(s, contact_id, receipt_pub)
    async with session_scope(ws) as s:
        assert await service.pending_posts_for_contact(s, contact_id) == []


async def test_post_delivery_is_exactly_once(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "PostOnce")
    await _contact(client, token)
    post_pub = await _post(client, token)
    fired = await client.post(f"/v0/outbound/posts/{post_pub}/fire", headers=_auth(token))
    assert fired.status_code == 200
    post_id = decode_public_id(IdPrefix.POST, post_pub)
    contacts: list[uuid.UUID] = []
    await service.run_post_snapshot(ws, post_id, enqueue=contacts.extend)
    (contact_id,) = contacts

    results = await asyncio.gather(
        *[
            service.deliver_post_receipt(workspace_id=ws, post_id=post_id, contact_id=contact_id)
            for _ in range(5)
        ]
    )
    assert results.count("delivered") == 1
    assert all(r == "already_processed" for r in results if r != "delivered")


async def test_chat_post_opens_a_conversation(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "Chats")
    contact_id = await _contact(client, token)
    post_pub = await _post(client, token, kind="chat")
    results = await _fire_and_deliver(client, token, ws, post_pub)
    assert results == ["delivered"]

    async with session_scope(ws) as s:
        convs = (
            await s.scalars(
                select(Conversation).where(
                    Conversation.contact_id == contact_id, Conversation.channel == "chat"
                )
            )
        ).all()
        receipt = (
            await s.execute(select(PostReceipt).where(PostReceipt.contact_id == contact_id))
        ).scalar_one()
    assert len(convs) == 1
    assert receipt.conversation_id == convs[0].id


async def test_post_consent_gate_at_delivery(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "PostConsent")
    contact_id = await _contact(client, token)
    subtype = (
        await client.post(
            "/v0/outbound/subscription-types",
            json={"name": "Announcements", "kind": "marketing"},
            headers=_auth(token),
        )
    ).json()["id"]
    # Unsubscribe the contact from this type before delivery.
    async with session_scope(ws) as s:
        await service.set_consent(
            s,
            workspace_id=ws,
            contact_id=contact_id,
            subscription_type_id=decode_public_id(IdPrefix.SUBSCRIPTION_TYPE, subtype),
            state="unsubscribed",
            source="api",
            actor_kind="admin",
        )
    post_pub = await _post(client, token, subtype_id=subtype)
    results = await _fire_and_deliver(client, token, ws, post_pub)
    assert results == ["skipped:unsubscribed"]

    async with session_scope(ws) as s:
        state = await s.scalar(
            select(PostReceipt.state).where(PostReceipt.contact_id == contact_id)
        )
    assert state == "suppressed_consent"
