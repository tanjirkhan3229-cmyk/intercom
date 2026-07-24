"""Integration tests for custom inbox views (P1.7 S3).

CRUD + validation, predicate-filtered listing (channel / attribute / compound), the cached count
badge, team-shared views, and cross-tenant RLS isolation.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from relay.core.db import session_scope
from relay.core.ids import IdPrefix, decode_public_id
from relay.core.redis import get_redis
from relay.modules.messaging.models import Conversation

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


async def _conversation(client: httpx.AsyncClient, tok: str, *, channel: str = "chat") -> dict:
    c = await client.post(
        "/v0/contacts/identify", json={"external_id": uuid4().hex}, headers=_auth(tok)
    )
    contact_id = c.json()["id"]
    r = await client.post(
        "/v0/conversations",
        json={"contact_id": contact_id, "body": "hi", "channel": channel},
        headers=_auth(tok),
    )
    assert r.status_code == 201, r.text
    return r.json()


def _ws_uuid(ws_pub: str) -> object:
    return decode_public_id(IdPrefix.WORKSPACE, ws_pub)


async def _create_view(client: httpx.AsyncClient, tok: str, name: str, filter_: dict) -> dict:
    r = await client.post("/v0/views", json={"name": name, "filter": filter_}, headers=_auth(tok))
    assert r.status_code == 201, r.text
    return r.json()


# --- CRUD + validation --------------------------------------------------------


async def test_view_crud(client: httpx.AsyncClient) -> None:
    tok, _ws = await _owner(client, "Views")
    view = await _create_view(
        client, tok, "Priority email", {"op": "eq", "field": "channel", "value": "email"}
    )
    assert view["name"] == "Priority email"
    assert (await client.get("/v0/views", headers=_auth(tok))).json()[0]["id"] == view["id"]

    upd = await client.put(
        f"/v0/views/{view['id']}",
        json={"name": "Renamed", "filter": {"op": "eq", "field": "priority", "value": True}},
        headers=_auth(tok),
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["name"] == "Renamed"

    assert (await client.delete(f"/v0/views/{view['id']}", headers=_auth(tok))).status_code == 204
    assert (await client.get(f"/v0/views/{view['id']}", headers=_auth(tok))).status_code == 404


async def test_view_rejects_bad_filter(client: httpx.AsyncClient) -> None:
    tok, _ws = await _owner(client, "ViewsBad")
    r = await client.post(
        "/v0/views",
        json={"name": "bad", "filter": {"op": "eq", "field": "no_such_field", "value": 1}},
        headers=_auth(tok),
    )
    assert r.status_code == 422, r.text


# --- filtered listing ---------------------------------------------------------


async def test_view_filters_by_channel(client: httpx.AsyncClient) -> None:
    tok, _ws = await _owner(client, "ViewsChan")
    await _conversation(client, tok, channel="chat")
    await _conversation(client, tok, channel="chat")
    email_conv = await _conversation(client, tok, channel="email")

    view = await _create_view(
        client,
        tok,
        "Email",
        {
            "op": "and",
            "clauses": [
                {"op": "eq", "field": "channel", "value": "email"},
                {"op": "eq", "field": "state", "value": "open"},
            ],
        },
    )
    listing = (await client.get(f"/v0/views/{view['id']}/conversations", headers=_auth(tok))).json()
    assert [c["id"] for c in listing["items"]] == [email_conv["id"]]

    count = (await client.get(f"/v0/views/{view['id']}/count", headers=_auth(tok))).json()
    assert count["count"] == 1


async def test_view_filters_by_attribute(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client, "ViewsAttr")
    gold = await _conversation(client, tok)
    await _conversation(client, tok)  # no attribute

    # Set a conversation attribute directly (no HTTP surface sets it in P0/P1.7).
    async with session_scope(_ws_uuid(ws)) as session:
        conv = await session.get(Conversation, decode_public_id(IdPrefix.CONVERSATION, gold["id"]))
        assert conv is not None
        conv.attributes = {"tier": "gold"}

    view = await _create_view(
        client, tok, "Gold", {"op": "eq", "field": "attributes.tier", "value": "gold"}
    )
    listing = (await client.get(f"/v0/views/{view['id']}/conversations", headers=_auth(tok))).json()
    assert [c["id"] for c in listing["items"]] == [gold["id"]]


async def test_view_count_is_cached(client: httpx.AsyncClient) -> None:
    tok, _ws = await _owner(client, "ViewsCache")
    await _conversation(client, tok, channel="email")
    view = await _create_view(
        client, tok, "Email", {"op": "eq", "field": "channel", "value": "email"}
    )
    first = (await client.get(f"/v0/views/{view['id']}/count", headers=_auth(tok))).json()
    assert first["count"] == 1

    # A new matching conversation does not change the cached badge until the TTL expires.
    await _conversation(client, tok, channel="email")
    cached = (await client.get(f"/v0/views/{view['id']}/count", headers=_auth(tok))).json()
    assert cached["count"] == 1  # served from Redis cache

    # After the cache key is dropped, the count reflects truth (2).
    await get_redis().delete(f"inbox:view:count:{view['id']}")
    fresh = (await client.get(f"/v0/views/{view['id']}/count", headers=_auth(tok))).json()
    assert fresh["count"] == 2


# --- cross-tenant RLS ---------------------------------------------------------


async def test_cross_tenant_isolation(client: httpx.AsyncClient) -> None:
    tok_a, _ws_a = await _owner(client, "Alpha")
    tok_b, _ws_b = await _owner(client, "Bravo")
    view_a = await _create_view(
        client, tok_a, "A", {"op": "eq", "field": "channel", "value": "email"}
    )
    assert (await client.get("/v0/views", headers=_auth(tok_b))).json() == []
    assert (await client.get(f"/v0/views/{view_a['id']}", headers=_auth(tok_b))).status_code == 404
    assert (
        await client.delete(f"/v0/views/{view_a['id']}", headers=_auth(tok_b))
    ).status_code == 404


# --- NULL semantics at the DB level (ne includes NULL; not_exists on a JSONB attribute) --------


async def _set_conv(ws_pub: str, conv_id: str, **fields: object) -> None:
    async with session_scope(_ws_uuid(ws_pub)) as session:
        conv = await session.get(Conversation, decode_public_id(IdPrefix.CONVERSATION, conv_id))
        assert conv is not None
        for key, value in fields.items():
            setattr(conv, key, value)


async def test_view_ne_includes_null_rows(client: httpx.AsyncClient) -> None:
    """``ne`` must match rows whose field is NULL (IS DISTINCT FROM), mirroring the Python
    evaluator — a plain ``!=`` would silently drop them."""
    tok, ws = await _owner(client, "ViewsNe")
    c_null = await _conversation(client, tok)  # ai_status IS NULL
    c_match = await _conversation(client, tok)
    await _set_conv(ws, c_match["id"], ai_status="handed_off")

    view = await _create_view(
        client, tok, "NotHandedOff", {"op": "ne", "field": "ai_status", "value": "handed_off"}
    )
    ids = [
        c["id"]
        for c in (
            await client.get(f"/v0/views/{view['id']}/conversations", headers=_auth(tok))
        ).json()["items"]
    ]
    assert c_null["id"] in ids  # NULL is "not equal" → included
    assert c_match["id"] not in ids  # the concrete match is excluded


async def test_view_not_exists_on_attribute(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client, "ViewsNotExists")
    c_with = await _conversation(client, tok)
    c_without = await _conversation(client, tok)
    await _set_conv(ws, c_with["id"], attributes={"tier": "gold"})

    view = await _create_view(
        client, tok, "NoTier", {"op": "not_exists", "field": "attributes.tier"}
    )
    ids = [
        c["id"]
        for c in (
            await client.get(f"/v0/views/{view['id']}/conversations", headers=_auth(tok))
        ).json()["items"]
    ]
    assert c_without["id"] in ids
    assert c_with["id"] not in ids
