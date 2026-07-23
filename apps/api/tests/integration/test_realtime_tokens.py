"""Realtime token/subscription authz + long-poll fallback (P0.4, RFC-001 §6.3, §10).

- Agents get an identity connection token and per-channel subscription tokens, but only for
  channels in their own workspace (a cross-tenant channel is refused).
- The long-poll fallback (``?after=``) returns ascending new parts and is gated by the
  ``realtime_fallback`` kill switch.
- Typing/presence are relayed best-effort (a down gateway never fails the request) and leave a
  Redis TTL key behind, never a Postgres row.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from relay.core import realtime
from relay.core.ids import IdPrefix, decode_public_id
from relay.core.redis import get_redis
from relay.settings import get_settings

pytestmark = pytest.mark.integration


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _owner(client: httpx.AsyncClient, name: str) -> tuple[str, str]:
    resp = await client.post(
        "/v0/auth/signup",
        json={
            "workspace_name": name,
            "email": f"owner-{uuid4().hex}@example.com",
            "password": "password123",
            "name": "Owner",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["access_token"], body["workspace"]["id"]


async def _conversation(client: httpx.AsyncClient, tok: str) -> dict[str, str]:
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


async def test_agent_connection_token(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client, "Alpha")
    resp = await client.post("/v0/realtime/token", headers=_auth(tok))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ws_url"] == get_settings().centrifugo_ws_url
    claims = realtime.decode_centrifugo_token(body["token"])
    assert claims["info"]["ws"] == ws
    assert "channels" not in claims


async def test_subscribe_only_authorizes_own_workspace_channels(client: httpx.AsyncClient) -> None:
    tok_a, ws_a = await _owner(client, "Alpha")
    tok_b, ws_b = await _owner(client, "Bravo")
    conv_a = await _conversation(client, tok_a)
    conv_b = await _conversation(client, tok_b)

    # Own conversation + own inbox → tokens minted.
    ok = await client.post(
        "/v0/realtime/subscribe",
        json={"channels": [f"conv:{conv_a['id']}", f"inbox:{ws_a}:all"]},
        headers=_auth(tok_a),
    )
    assert ok.status_code == 200, ok.text
    assert set(ok.json()["tokens"]) == {f"conv:{conv_a['id']}", f"inbox:{ws_a}:all"}

    # Another workspace's conversation → 404 (RLS scopes the conversation lookup).
    cross_conv = await client.post(
        "/v0/realtime/subscribe",
        json={"channels": [f"conv:{conv_b['id']}"]},
        headers=_auth(tok_a),
    )
    assert cross_conv.status_code == 404

    # Another workspace's inbox channel → 403.
    cross_inbox = await client.post(
        "/v0/realtime/subscribe",
        json={"channels": [f"inbox:{ws_b}:all"]},
        headers=_auth(tok_a),
    )
    assert cross_inbox.status_code == 403


async def test_widget_token_cannot_subscribe_to_another_conversation(
    client: httpx.AsyncClient,
) -> None:
    """Acceptance: a widget token pinned to conversation A cannot reach conversation B's channel."""
    tok, ws = await _owner(client, "Alpha")
    conv_a = await _conversation(client, tok)
    conv_b = await _conversation(client, tok)

    token = realtime.widget_connection_token(
        workspace_id=decode_public_id(IdPrefix.WORKSPACE, ws),
        contact_id=decode_public_id(IdPrefix.CONTACT, conv_a["contact_id"]),
        conversation_id=decode_public_id(IdPrefix.CONVERSATION, conv_a["id"]),
    )
    claims = realtime.decode_centrifugo_token(token)
    assert claims["channels"] == [f"conv:{conv_a['id']}"]
    assert f"conv:{conv_b['id']}" not in claims["channels"]


async def test_long_poll_after_returns_ascending_new_parts(client: httpx.AsyncClient) -> None:
    tok, _ws = await _owner(client, "Alpha")
    conv = await _conversation(client, tok)  # opens with the contact's first comment
    initial = (await client.get(f"/v0/conversations/{conv['id']}/parts", headers=_auth(tok))).json()
    first_part_id = initial["items"][-1]["id"]  # oldest (list is newest-first)

    reply = (
        await client.post(
            f"/v0/conversations/{conv['id']}/reply", json={"body": "hello"}, headers=_auth(tok)
        )
    ).json()

    poll = (
        await client.get(
            f"/v0/conversations/{conv['id']}/parts",
            params={"after": first_part_id},
            headers=_auth(tok),
        )
    ).json()
    ids = [p["id"] for p in poll["items"]]
    assert reply["id"] in ids
    assert first_part_id not in ids  # strictly after
    times = [p["created_at"] for p in poll["items"]]
    assert times == sorted(times)  # ascending / chronological


async def test_long_poll_gated_by_realtime_fallback_flag(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tok, _ws = await _owner(client, "Alpha")
    conv = await _conversation(client, tok)
    initial = (await client.get(f"/v0/conversations/{conv['id']}/parts", headers=_auth(tok))).json()
    part_id = initial["items"][-1]["id"]

    monkeypatch.setattr(get_settings(), "realtime_fallback", False)
    blocked = await client.get(
        f"/v0/conversations/{conv['id']}/parts",
        params={"after": part_id},
        headers=_auth(tok),
    )
    assert blocked.status_code == 403


async def test_typing_relay_is_best_effort_and_leaves_redis_ttl(
    client: httpx.AsyncClient,
) -> None:
    tok, _ws = await _owner(client, "Alpha")
    conv = await _conversation(client, tok)
    # No gateway under test → the Centrifugo publish fails, but typing is best-effort: still 204.
    resp = await client.post(f"/v0/conversations/{conv['id']}/typing", json={}, headers=_auth(tok))
    assert resp.status_code == 204
    redis = get_redis()
    keys = [key async for key in redis.scan_iter(f"rt:typing:{conv['id']}:*")]
    assert keys, "typing indicator should be recorded in Redis with a TTL"
    assert await redis.ttl(keys[0]) > 0  # ephemeral, never persisted to Postgres
