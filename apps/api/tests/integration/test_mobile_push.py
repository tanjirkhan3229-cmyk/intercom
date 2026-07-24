"""Mobile push backend integration tests (P1.10, RFC-000 §2.1).

Covers the server-side acceptance the mobile SDKs depend on:
- **Device registration + rotation**: an SDK registers its APNs/FCM token as the authenticated
  contact; re-registering the same token upserts (rotation), a new token adds a row, unregister
  removes it.
- **Fan-out**: an agent/AI reply pushes to the contact's active devices (deep-linked to the
  conversation); a contact's own message never pushes; a second run is deduped (exactly-once).
- **Token invalidation**: a provider-rejected token is marked ``stale`` and dropped from fan-out.
- **Tenancy**: device tokens are workspace-isolated under RLS.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from relay.core.db import session_scope
from relay.core.ids import IdPrefix, decode_public_id
from relay.modules.messaging import push, push_service
from relay.modules.messaging.models import DeviceToken

pytestmark = pytest.mark.integration

PASSWORD = "password123"


@pytest.fixture
async def widget_client(app_instance) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app_instance)
    async with httpx.AsyncClient(transport=transport, base_url="https://widget.test") as c:
        yield c


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _owner(client: httpx.AsyncClient) -> tuple[str, str]:
    """Sign up an owner; return (agent_access_token, workspace_public_id == widget app_id)."""
    resp = await client.post(
        "/v0/auth/signup",
        json={
            "workspace_name": f"ws-{uuid4().hex}",
            "email": f"owner-{uuid4().hex}@example.com",
            "password": PASSWORD,
            "name": "Owner",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["access_token"], body["workspace"]["id"]


async def _boot(client: httpx.AsyncClient, app_id: str) -> dict[str, str]:
    """Boot an anonymous lead; return the contact-session auth header."""
    boot = (await client.post("/v0/widget/boot", json={"app_id": app_id})).json()
    return _auth(boot["session_token"])


async def _count_devices(app_id: str) -> int:
    ws = decode_public_id(IdPrefix.WORKSPACE, app_id)
    async with session_scope(ws) as session:
        return int(await session.scalar(select(func.count()).select_from(DeviceToken)) or 0)


async def _agent_reply(client: httpx.AsyncClient, agent_tok: str, conv_id: str, body: str) -> str:
    """Post an agent reply (admin comment) and return its part public id."""
    resp = await client.post(
        f"/v0/conversations/{conv_id}/reply", json={"body": body}, headers=_auth(agent_tok)
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _fanout(app_id: str, conv_id: str, part_id: str) -> int:
    return await push_service.fanout_push_for_part(
        workspace_id=decode_public_id(IdPrefix.WORKSPACE, app_id),
        conversation_id=decode_public_id(IdPrefix.CONVERSATION, conv_id),
        part_id=decode_public_id(IdPrefix.PART, part_id),
    )


# --- Registration + rotation --------------------------------------------------


async def test_register_rotate_and_unregister(widget_client: httpx.AsyncClient) -> None:
    _, app_id = await _owner(widget_client)
    h = await _boot(widget_client, app_id)

    r1 = await widget_client.post(
        "/v0/widget/devices", json={"platform": "ios", "token": "tok-1"}, headers=h
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["platform"] == "ios" and r1.json()["status"] == "active"
    dev_id = r1.json()["id"]

    # Re-register the SAME token (rotation / refresh) → upsert, same row, no duplicate.
    r1b = await widget_client.post(
        "/v0/widget/devices", json={"platform": "ios", "token": "tok-1"}, headers=h
    )
    assert r1b.json()["id"] == dev_id
    assert await _count_devices(app_id) == 1

    # A different token adds a distinct row.
    r2 = await widget_client.post(
        "/v0/widget/devices", json={"platform": "android", "token": "tok-2"}, headers=h
    )
    assert r2.json()["id"] != dev_id
    assert await _count_devices(app_id) == 2

    # Unregister (logout) is idempotent.
    d1 = await widget_client.delete("/v0/widget/devices", params={"token": "tok-1"}, headers=h)
    assert d1.status_code == 204
    d2 = await widget_client.delete("/v0/widget/devices", params={"token": "tok-1"}, headers=h)
    assert d2.status_code == 204  # already gone → still 204
    assert await _count_devices(app_id) == 1


async def test_register_requires_contact_session(widget_client: httpx.AsyncClient) -> None:
    agent_tok, _app_id = await _owner(widget_client)
    # An agent access token is the wrong audience for a widget route.
    resp = await widget_client.post(
        "/v0/widget/devices",
        json={"platform": "ios", "token": "tok"},
        headers=_auth(agent_tok),
    )
    assert resp.status_code == 401, resp.text


async def test_register_rejects_bad_platform(widget_client: httpx.AsyncClient) -> None:
    _, app_id = await _owner(widget_client)
    h = await _boot(widget_client, app_id)
    resp = await widget_client.post(
        "/v0/widget/devices", json={"platform": "web", "token": "tok"}, headers=h
    )
    assert resp.status_code == 422, resp.text


# --- Fan-out ------------------------------------------------------------------


async def test_agent_reply_pushes_to_device_and_dedupes(widget_client: httpx.AsyncClient) -> None:
    push.reset_pusher()
    fake = push.fake_pusher()
    agent_tok, app_id = await _owner(widget_client)
    h = await _boot(widget_client, app_id)

    await widget_client.post(
        "/v0/widget/devices", json={"platform": "ios", "token": "tok-ios"}, headers=h
    )
    conv_id = (
        await widget_client.post("/v0/widget/conversations", json={"body": "help me"}, headers=h)
    ).json()["id"]
    part_id = await _agent_reply(widget_client, agent_tok, conv_id, "Hi — happy to help!")

    n = await _fanout(app_id, conv_id, part_id)
    assert n == 1
    assert len(fake.sent) == 1
    sent = fake.sent[0]
    assert sent.token == "tok-ios"
    assert sent.body == "Hi — happy to help!"  # the agent reply, not the contact's message
    assert sent.data["conversation_id"] == conv_id  # deep-link payload

    # At-least-once: a redelivery of the same part sends nothing more (push_receipts gate).
    again = await _fanout(app_id, conv_id, part_id)
    assert again == 0
    assert len(fake.sent) == 1


async def test_contact_message_does_not_push(widget_client: httpx.AsyncClient) -> None:
    push.reset_pusher()
    fake = push.fake_pusher()
    _, app_id = await _owner(widget_client)
    h = await _boot(widget_client, app_id)

    await widget_client.post(
        "/v0/widget/devices", json={"platform": "ios", "token": "tok-ios"}, headers=h
    )
    conv_id = (
        await widget_client.post("/v0/widget/conversations", json={"body": "hello"}, headers=h)
    ).json()["id"]
    # The contact's own first message is a part — fanning out on it must push nothing.
    parts = (
        await widget_client.get(f"/v0/widget/conversations/{conv_id}/parts", headers=h)
    ).json()["items"]
    contact_part = next(p for p in parts if p["author_kind"] == "contact")

    n = await _fanout(app_id, conv_id, contact_part["id"])
    assert n == 0
    assert fake.sent == []


async def test_invalid_token_marked_stale(widget_client: httpx.AsyncClient) -> None:
    push.reset_pusher()
    fake = push.fake_pusher()
    fake.invalid.add("tok-dead")
    agent_tok, app_id = await _owner(widget_client)
    h = await _boot(widget_client, app_id)

    await widget_client.post(
        "/v0/widget/devices", json={"platform": "android", "token": "tok-dead"}, headers=h
    )
    conv_id = (
        await widget_client.post("/v0/widget/conversations", json={"body": "hi"}, headers=h)
    ).json()["id"]
    part_id = await _agent_reply(widget_client, agent_tok, conv_id, "reply")

    n = await _fanout(app_id, conv_id, part_id)
    assert n == 0  # nothing delivered — the provider rejected the token

    ws = decode_public_id(IdPrefix.WORKSPACE, app_id)
    async with session_scope(ws) as session:
        status = await session.scalar(
            select(DeviceToken.status).where(DeviceToken.token == "tok-dead")
        )
    assert status == "stale"


# --- Tenancy ------------------------------------------------------------------


async def test_device_tokens_isolated_across_workspaces(app_instance) -> None:
    transport = httpx.ASGITransport(app=app_instance)
    async with (
        httpx.AsyncClient(transport=transport, base_url="https://widget.test") as ca,
        httpx.AsyncClient(transport=transport, base_url="https://widget.test") as cb,
    ):
        _, app_a = await _owner(ca)
        _, app_b = await _owner(cb)
        ha = await _boot(ca, app_a)
        hb = await _boot(cb, app_b)

        # The same physical token can legitimately exist in two workspaces (two apps, one device).
        await ca.post("/v0/widget/devices", json={"platform": "ios", "token": "shared"}, headers=ha)
        await cb.post("/v0/widget/devices", json={"platform": "ios", "token": "shared"}, headers=hb)

        # Each workspace sees exactly one device under RLS — never the other's.
        assert await _count_devices(app_a) == 1
        assert await _count_devices(app_b) == 1
