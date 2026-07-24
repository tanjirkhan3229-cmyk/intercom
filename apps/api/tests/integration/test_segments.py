"""Segments integration tests (P1.9, RFC-002 §5.4).

Exercises the real API + the ``segment-refresh`` delta consumer over the outbox stream:
- CRUD + live ``preview`` count + materialised members;
- **the acceptance**: membership converges after an attribute flip via the delta path (the outbox
  consumer), WITHOUT running the nightly reconcile; the reverse flip removes it; the delta is
  idempotent (level-triggered);
- cross-tenant isolation (RLS on; unset ``app.ws`` → zero rows);
- RBAC (AGENT may preview, only ADMIN may create).
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import psycopg
import pytest
from sqlalchemy import func, select

from relay.core import outbox_relay
from relay.core.db import get_sessionmaker, session_scope
from relay.core.errors import PermissionDeniedError
from relay.core.ids import IdPrefix, decode_public_id, uuid7
from relay.core.principal import Principal
from relay.core.rbac import Role
from relay.core.redis import get_redis, get_redis_sync
from relay.modules.crm import schemas
from relay.modules.crm import segments_consumer as sc
from relay.modules.crm import service as crm_service
from relay.modules.crm.models import Segment, SegmentMember
from relay.settings import get_settings

pytestmark = pytest.mark.integration

PASSWORD = "password123"


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


def _drain_outbox() -> None:
    """Publish all pending outbox rows to the Redis stream (what ``relay outbox-relay`` does)."""
    dsn = get_settings().database_url_psycopg
    redis = get_redis_sync()
    with psycopg.connect(dsn) as conn:
        conn.autocommit = False
        outbox_relay.drain(conn, redis)


async def _drain_and_refresh() -> None:
    """Drain the outbox to the stream, then run the segment-refresh consumer over it once."""
    _drain_outbox()
    redis = get_redis()
    await sc.ensure_group(redis)
    while (await sc.consume_once(redis, count=1000)).entries_read:
        pass


async def _define_plan_attr(client: httpx.AsyncClient, tok: str) -> None:
    resp = await client.post(
        "/v0/attribute-definitions",
        json={"entity": "contact", "name": "plan", "data_type": "string"},
        headers=_auth(tok),
    )
    assert resp.status_code == 201, resp.text


async def _identify(client: httpx.AsyncClient, tok: str, ext: str, plan: str) -> str:
    resp = await client.post(
        "/v0/contacts/identify",
        json={"external_id": ext, "custom": {"plan": plan}},
        headers=_auth(tok),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def test_segment_crud_and_preview(client: httpx.AsyncClient) -> None:
    tok, _ws = await _owner(client, "SegCRUD")
    await _define_plan_attr(client, tok)
    await _identify(client, tok, "pro-1", "pro")
    await _identify(client, tok, "pro-2", "pro")
    await _identify(client, tok, "free-1", "free")

    predicate = {"op": "eq", "field": "custom.plan", "value": "pro"}

    # Preview (no persistence) returns the live count.
    prev = await client.post(
        "/v0/segments/preview", json={"predicate": predicate}, headers=_auth(tok)
    )
    assert prev.status_code == 200, prev.text
    assert prev.json()["count"] == 2

    # Create → cached count == live count.
    created = await client.post(
        "/v0/segments",
        json={"name": "Pro users", "description": "on the pro plan", "predicate": predicate},
        headers=_auth(tok),
    )
    assert created.status_code == 201, created.text
    seg = created.json()
    assert seg["cached_member_count"] == 2
    assert seg["predicate"] == predicate

    # Get + list.
    got = await client.get(f"/v0/segments/{seg['id']}", headers=_auth(tok))
    assert got.status_code == 200 and got.json()["name"] == "Pro users"
    listed = await client.get("/v0/segments", headers=_auth(tok))
    assert listed.status_code == 200 and len(listed.json()["items"]) == 1

    # Update predicate → count recomputed.
    upd = await client.patch(
        f"/v0/segments/{seg['id']}",
        json={"predicate": {"op": "eq", "field": "custom.plan", "value": "free"}},
        headers=_auth(tok),
    )
    assert upd.status_code == 200 and upd.json()["cached_member_count"] == 1

    # Duplicate name → 409.
    dup = await client.post(
        "/v0/segments", json={"name": "Pro users", "predicate": {}}, headers=_auth(tok)
    )
    assert dup.status_code == 409, dup.text

    # Delete.
    assert (await client.delete(f"/v0/segments/{seg['id']}", headers=_auth(tok))).status_code == 204
    assert (await client.get(f"/v0/segments/{seg['id']}", headers=_auth(tok))).status_code == 404


async def test_membership_converges_after_attribute_flip(client: httpx.AsyncClient) -> None:
    """P1.9 acceptance: the delta path (outbox consumer), not the nightly reconcile, moves it."""
    tok, ws_pub = await _owner(client, "SegDelta")
    ws = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    await _define_plan_attr(client, tok)
    contact_pub = await _identify(client, tok, "u1", "free")
    contact_id = decode_public_id(IdPrefix.CONTACT, contact_pub)

    seg = (
        await client.post(
            "/v0/segments",
            json={"name": "Pro", "predicate": {"op": "eq", "field": "custom.plan", "value": "pro"}},
            headers=_auth(tok),
        )
    ).json()
    assert seg["cached_member_count"] == 0  # free contact does not match

    # Flip the contact to 'pro' → emits crm.contact.updated → delta consumer adds it.
    resp = await client.patch(
        f"/v0/contacts/{contact_pub}", json={"custom": {"plan": "pro"}}, headers=_auth(tok)
    )
    assert resp.status_code == 200, resp.text
    await _drain_and_refresh()

    members = await client.get(f"/v0/segments/{seg['id']}/members", headers=_auth(tok))
    assert members.status_code == 200
    assert [m["id"] for m in members.json()["items"]] == [contact_pub]
    assert (await client.get(f"/v0/segments/{seg['id']}", headers=_auth(tok))).json()[
        "cached_member_count"
    ] == 1

    # Level-triggered idempotency: re-running the delta for the same contact changes nothing.
    async with session_scope(ws) as s:
        await crm_service.refresh_contact_segments(s, contact_id)
    async with session_scope(ws) as s:
        n = await s.scalar(select(func.count()).select_from(SegmentMember))
        seg_row = await s.get(Segment, decode_public_id(IdPrefix.SEGMENT, seg["id"]))
    assert n == 1
    assert seg_row is not None and seg_row.cached_member_count == 1

    # Flip back to 'free' → delta removes it.
    resp = await client.patch(
        f"/v0/contacts/{contact_pub}", json={"custom": {"plan": "free"}}, headers=_auth(tok)
    )
    assert resp.status_code == 200, resp.text
    await _drain_and_refresh()

    members = await client.get(f"/v0/segments/{seg['id']}/members", headers=_auth(tok))
    assert members.json()["items"] == []
    assert (await client.get(f"/v0/segments/{seg['id']}", headers=_auth(tok))).json()[
        "cached_member_count"
    ] == 0


async def test_cross_tenant_isolation(client: httpx.AsyncClient) -> None:
    tok_a, ws_a = await _owner(client, "SegA")
    tok_b, _ws_b = await _owner(client, "SegB")
    await _define_plan_attr(client, tok_a)
    await _identify(client, tok_a, "a1", "pro")

    seg_a = (
        await client.post(
            "/v0/segments",
            json={
                "name": "A pros",
                "predicate": {"op": "eq", "field": "custom.plan", "value": "pro"},
            },
            headers=_auth(tok_a),
        )
    ).json()

    # B cannot read A's segment (RLS → 404) and sees none of its own.
    assert (
        await client.get(f"/v0/segments/{seg_a['id']}", headers=_auth(tok_b))
    ).status_code == 404
    assert (await client.get("/v0/segments", headers=_auth(tok_b))).json()["items"] == []
    # B's "all contacts" preview never counts A's contact.
    prev_b = await client.post("/v0/segments/preview", json={"predicate": {}}, headers=_auth(tok_b))
    assert prev_b.json()["count"] == 0

    # RLS backstop: with no app.ws set, segments + members return zero rows.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s, s.begin():
        assert (await s.scalar(select(func.count()).select_from(Segment))) == 0
        assert (await s.scalar(select(func.count()).select_from(SegmentMember))) == 0
    # sanity: with A's GUC set, A's segment is visible.
    async with session_scope(decode_public_id(IdPrefix.WORKSPACE, ws_a)) as s:
        assert (await s.scalar(select(func.count()).select_from(Segment))) == 1


async def test_rbac_agent_can_preview_not_create(client: httpx.AsyncClient) -> None:
    _tok, ws_pub = await _owner(client, "SegRBAC")
    ws = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    agent = Principal(admin_id=uuid7(), workspace_id=ws, role=Role.AGENT)

    async with session_scope(ws) as s:
        # AGENT may preview.
        count = await crm_service.preview_segment(
            s, agent, schemas.SegmentPreviewRequest(predicate={})
        )
        assert count == 0
        # AGENT may not create.
        with pytest.raises(PermissionDeniedError):
            await crm_service.create_segment(
                s, agent, schemas.SegmentCreate(name="nope", predicate={})
            )
