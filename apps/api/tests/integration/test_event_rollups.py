"""Event rollups + event-count segments (P1.9, RFC-002 §5.4).

Proves the "segments never scan raw events" substrate end to end: track events → drain to the
``events`` firehose → ``relay_event_rollup`` aggregates them per contact/day (idempotently) → an
``event.<name>.count`` segment predicate compiles to a correlated SUM over ``event_rollups`` and
selects the right contacts (preview + materialised reconcile).
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from relay.core.db import session_scope
from relay.core.ids import IdPrefix, decode_public_id
from relay.modules.crm import service as crm_service
from relay.modules.crm.models import EventRollup, Segment
from relay.modules.crm.tasks import compute_event_rollups, drain_events

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


async def _identify(client: httpx.AsyncClient, tok: str, ext: str) -> str:
    resp = await client.post("/v0/contacts/identify", json={"external_id": ext}, headers=_auth(tok))
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _track(client: httpx.AsyncClient, tok: str, contact_pub: str, name: str, n: int) -> None:
    events = [{"name": name, "contact_id": contact_pub, "properties": {"i": i}} for i in range(n)]
    resp = await client.post("/v0/events/track", json={"events": events}, headers=_auth(tok))
    assert resp.status_code == 202, resp.text


async def _rollup_counts(ws) -> dict[tuple, int]:
    async with session_scope(ws) as s:
        rows = (
            await s.execute(
                select(EventRollup.contact_id, EventRollup.event_name, EventRollup.count)
            )
        ).all()
    return {(r[0], r[1]): r[2] for r in rows}


async def test_rollups_are_idempotent_and_feed_event_segments(client: httpx.AsyncClient) -> None:
    tok, ws_pub = await _owner(client, "Rollups")
    ws = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    c1 = await _identify(client, tok, "c1")
    c2 = await _identify(client, tok, "c2")
    c1_id = decode_public_id(IdPrefix.CONTACT, c1)
    c2_id = decode_public_id(IdPrefix.CONTACT, c2)

    await _track(client, tok, c1, "purchased", 3)
    await _track(client, tok, c2, "purchased", 1)

    # Land events, then roll them up.
    assert drain_events() == 4
    written = compute_event_rollups()
    assert sum(written.values()) >= 2  # at least the two (contact, purchased, today) grains

    counts = await _rollup_counts(ws)
    assert counts[(c1_id, "purchased")] == 3
    assert counts[(c2_id, "purchased")] == 1

    # Idempotent: a second full-day recompute yields identical rows.
    compute_event_rollups()
    assert await _rollup_counts(ws) == counts

    # An event-count segment selects only the contact over threshold.
    predicate = {"op": "gte", "field": "event.purchased.count", "value": 3}
    prev = await client.post(
        "/v0/segments/preview", json={"predicate": predicate}, headers=_auth(tok)
    )
    assert prev.status_code == 200 and prev.json()["count"] == 1

    created = await client.post(
        "/v0/segments", json={"name": "Buyers", "predicate": predicate}, headers=_auth(tok)
    )
    assert created.status_code == 201, created.text
    seg = created.json()
    assert seg["cached_member_count"] == 1

    # Nightly reconcile materialises exactly that contact.
    reconciled = await crm_service.reconcile_all_segments()
    assert reconciled >= 1
    members = await client.get(f"/v0/segments/{seg['id']}/members", headers=_auth(tok))
    assert [m["id"] for m in members.json()["items"]] == [c1]

    # A windowed predicate (last 1 day) also matches (events were tracked "now").
    windowed = {"op": "gte", "field": "event.purchased.count_1d", "value": 3}
    prev_w = await client.post(
        "/v0/segments/preview", json={"predicate": windowed}, headers=_auth(tok)
    )
    assert prev_w.json()["count"] == 1

    # A segment referencing a never-emitted event has no members.
    none_pred = {"op": "gte", "field": "event.churned.count", "value": 1}
    prev_none = await client.post(
        "/v0/segments/preview", json={"predicate": none_pred}, headers=_auth(tok)
    )
    assert prev_none.json()["count"] == 0

    async with session_scope(ws) as s:
        assert (await s.scalar(select(func.count()).select_from(Segment))) == 1
