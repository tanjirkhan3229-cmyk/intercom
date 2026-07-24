"""Integration tests for collision detection (P1.7 S5).

Two agents with the same conversation open both appear in its presence (the inbox soft-lock);
typing shows in ``typers``; state is Redis-TTL only (never persisted); presence is isolated per
conversation and workspace-scoped.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from relay.core import realtime
from relay.core.ids import IdPrefix, encode_public_id
from relay.core.redis import get_redis

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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


async def _conversation(client: httpx.AsyncClient, tok: str) -> dict:
    contact = (
        await client.post(
            "/v0/contacts/identify", json={"external_id": uuid4().hex}, headers=_auth(tok)
        )
    ).json()
    return (
        await client.post(
            "/v0/conversations",
            json={"contact_id": contact["id"], "body": "hi"},
            headers=_auth(tok),
        )
    ).json()


async def test_two_agents_viewing_collide(client: httpx.AsyncClient) -> None:
    tok, _ws = await _owner(client, "Collide")
    conv = await _conversation(client, tok)

    # The owner opens the conversation.
    r = await client.post(f"/v0/conversations/{conv['id']}/viewing", headers=_auth(tok))
    assert r.status_code == 204
    pres = (await client.get(f"/v0/conversations/{conv['id']}/presence", headers=_auth(tok))).json()
    assert len(pres["viewers"]) == 1
    owner_pub = pres["viewers"][0]

    # A second agent opens the same conversation (simulate their heartbeat).
    second = encode_public_id(IdPrefix.ADMIN, uuid4())
    await realtime.relay_viewing(conv["id"], admin_public_id=second)

    pres2 = (
        await client.get(f"/v0/conversations/{conv['id']}/presence", headers=_auth(tok))
    ).json()
    assert set(pres2["viewers"]) == {owner_pub, second}  # both agents seen → collision warning


async def test_typing_shows_in_presence(client: httpx.AsyncClient) -> None:
    tok, _ws = await _owner(client, "CollideTyping")
    conv = await _conversation(client, tok)
    await client.post(f"/v0/conversations/{conv['id']}/typing", json={}, headers=_auth(tok))
    pres = (await client.get(f"/v0/conversations/{conv['id']}/presence", headers=_auth(tok))).json()
    assert any(t["actor_kind"] == "admin" for t in pres["typers"])


async def test_viewing_is_redis_only_with_ttl(client: httpx.AsyncClient) -> None:
    tok, _ws = await _owner(client, "CollideTTL")
    conv = await _conversation(client, tok)
    await client.post(f"/v0/conversations/{conv['id']}/viewing", headers=_auth(tok))
    redis = get_redis()
    keys = [key async for key in redis.scan_iter(f"rt:view:{conv['id']}:*")]
    assert keys, "viewing presence should be recorded in Redis"
    assert await redis.ttl(keys[0]) > 0  # ephemeral, never persisted to Postgres


async def test_presence_isolated_per_conversation(client: httpx.AsyncClient) -> None:
    tok, _ws = await _owner(client, "CollideIso")
    conv_a = await _conversation(client, tok)
    conv_b = await _conversation(client, tok)
    await client.post(f"/v0/conversations/{conv_a['id']}/viewing", headers=_auth(tok))
    pres_b = (
        await client.get(f"/v0/conversations/{conv_b['id']}/presence", headers=_auth(tok))
    ).json()
    assert pres_b["viewers"] == []


async def test_presence_cross_tenant_404(client: httpx.AsyncClient) -> None:
    tok_a, _a = await _owner(client, "CollideA")
    tok_b, _b = await _owner(client, "CollideB")
    conv = await _conversation(client, tok_a)
    assert (
        await client.get(f"/v0/conversations/{conv['id']}/presence", headers=_auth(tok_b))
    ).status_code == 404
    assert (
        await client.post(f"/v0/conversations/{conv['id']}/viewing", headers=_auth(tok_b))
    ).status_code == 404
