"""CRM integration tests (P0.2 acceptance, RFC-002 §5.4).

Covers: identify idempotency + merge, custom-attribute validation (422 on unknown/mismatch),
keyset pagination, the events firehose (10k batch landing via COPY through the drain), the
trigram-typeahead EXPLAIN plan, and cross-tenant isolation of the new tenant tables.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, text

from relay.core.db import session_scope
from relay.core.ids import IdPrefix, decode_public_id

pytestmark = pytest.mark.integration

PASSWORD = "password123"


async def _owner(client: httpx.AsyncClient, ws_name: str) -> tuple[str, str]:
    """Sign up an owner; return (access_token, workspace_public_id)."""
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


# --- identify (W2) ------------------------------------------------------------


async def test_identify_idempotent_same_external_id(client: httpx.AsyncClient) -> None:
    """Acceptance: identify twice with the same external_id ⇒ exactly one contact."""
    tok, _ = await _owner(client, "Acme")

    r1 = await client.post(
        "/v0/contacts/identify",
        json={"external_id": "u1", "name": "Alice", "email": "alice@example.com"},
        headers=_auth(tok),
    )
    assert r1.status_code == 200, r1.text
    c1 = r1.json()

    r2 = await client.post(
        "/v0/contacts/identify",
        json={"external_id": "u1", "name": "Alice R", "phone": "+15551234"},
        headers=_auth(tok),
    )
    assert r2.status_code == 200, r2.text
    c2 = r2.json()

    assert c1["id"] == c2["id"]  # same contact row
    assert c2["name"] == "Alice R"  # provided field overwrites
    assert c2["email"] == "alice@example.com"  # unprovided field preserved (merge)
    assert c2["phone"] == "+15551234"

    listing = (await client.get("/v0/contacts", headers=_auth(tok))).json()
    assert len(listing["items"]) == 1  # exactly one row


async def test_identify_by_email_when_no_external_id(client: httpx.AsyncClient) -> None:
    tok, _ = await _owner(client, "EmailKey")
    r1 = await client.post(
        "/v0/contacts/identify", json={"email": "bob@example.com"}, headers=_auth(tok)
    )
    assert r1.status_code == 200
    r2 = await client.post(
        "/v0/contacts/identify",
        json={"email": "bob@example.com", "name": "Bob"},
        headers=_auth(tok),
    )
    assert r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]
    assert r2.json()["name"] == "Bob"


async def test_identify_requires_an_identifier(client: httpx.AsyncClient) -> None:
    tok, _ = await _owner(client, "NoId")
    resp = await client.post("/v0/contacts/identify", json={"name": "Nobody"}, headers=_auth(tok))
    assert resp.status_code == 422  # neither external_id nor email


# --- custom attribute validation (the swamp guard) ----------------------------


async def test_custom_type_mismatch_rejected(client: httpx.AsyncClient) -> None:
    """Acceptance: a type-mismatched custom attribute is rejected with 422."""
    tok, _ = await _owner(client, "Attrs")
    d = await client.post(
        "/v0/attribute-definitions",
        json={"entity": "contact", "name": "age", "data_type": "number"},
        headers=_auth(tok),
    )
    assert d.status_code == 201, d.text

    bad = await client.post(
        "/v0/contacts/identify",
        json={"external_id": "u1", "custom": {"age": "not-a-number"}},
        headers=_auth(tok),
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "validation_error"

    good = await client.post(
        "/v0/contacts/identify",
        json={"external_id": "u1", "custom": {"age": 30}},
        headers=_auth(tok),
    )
    assert good.status_code == 200
    assert good.json()["custom"]["age"] == 30


async def test_unknown_custom_attribute_rejected(client: httpx.AsyncClient) -> None:
    tok, _ = await _owner(client, "Attrs2")
    bad = await client.post(
        "/v0/contacts/identify",
        json={"external_id": "u1", "custom": {"undefined_key": "x"}},
        headers=_auth(tok),
    )
    assert bad.status_code == 422


# --- keyset pagination --------------------------------------------------------


async def test_contacts_keyset_pagination(client: httpx.AsyncClient) -> None:
    tok, _ = await _owner(client, "Paged")
    for i in range(5):
        await client.post(
            "/v0/contacts/identify", json={"external_id": f"u{i}"}, headers=_auth(tok)
        )

    page1 = (await client.get("/v0/contacts?limit=2", headers=_auth(tok))).json()
    assert len(page1["items"]) == 2
    assert page1["next_cursor"] is not None

    page2 = (
        await client.get(f"/v0/contacts?limit=2&cursor={page1['next_cursor']}", headers=_auth(tok))
    ).json()
    assert len(page2["items"]) == 2
    # Disjoint pages (keyset, no overlap).
    ids1 = {c["id"] for c in page1["items"]}
    ids2 = {c["id"] for c in page2["items"]}
    assert ids1.isdisjoint(ids2)


# --- events firehose (W3) -----------------------------------------------------


async def test_track_10k_events_land_via_copy(client: httpx.AsyncClient) -> None:
    """Acceptance: a 10k-event batch lands via the COPY drain (one txn per chunk)."""
    from relay.modules.crm.models import Event
    from relay.modules.crm.tasks import drain_events

    tok, ws = await _owner(client, "Firehose")
    contact = (
        await client.post("/v0/contacts/identify", json={"external_id": "u1"}, headers=_auth(tok))
    ).json()

    events = [
        {"name": "page_view", "contact_id": contact["id"], "properties": {"i": i}}
        for i in range(10_000)
    ]
    resp = await client.post("/v0/events/track", json={"events": events}, headers=_auth(tok))
    assert resp.status_code == 202, resp.text
    assert resp.json()["accepted"] == 10_000

    # Drain synchronously (Celery task run in-process): temp-stage COPY + INSERT…SELECT.
    drained = drain_events()
    assert drained == 10_000

    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
    async with session_scope(ws_uuid) as session:
        count = await session.scalar(select(func.count()).select_from(Event))
    assert count == 10_000


async def test_track_rejects_unknown_contact(client: httpx.AsyncClient) -> None:
    tok, _ = await _owner(client, "BadTrack")
    fake_contact = "usr_0000000000000000000000"
    resp = await client.post(
        "/v0/events/track",
        json={"events": [{"name": "x", "contact_id": fake_contact}]},
        headers=_auth(tok),
    )
    assert resp.status_code in (404, 422)


async def test_events_are_workspace_isolated(client: httpx.AsyncClient) -> None:
    from relay.modules.crm.models import Event
    from relay.modules.crm.tasks import drain_events

    tok_a, ws_a = await _owner(client, "EvtA")
    _tok_b, ws_b = await _owner(client, "EvtB")
    contact_a = (
        await client.post("/v0/contacts/identify", json={"external_id": "u1"}, headers=_auth(tok_a))
    ).json()
    await client.post(
        "/v0/events/track",
        json={"events": [{"name": "seen", "contact_id": contact_a["id"]}]},
        headers=_auth(tok_a),
    )
    drain_events()

    async with session_scope(decode_public_id(IdPrefix.WORKSPACE, ws_a)) as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 1
    async with session_scope(decode_public_id(IdPrefix.WORKSPACE, ws_b)) as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 0


# --- trigram typeahead plan ---------------------------------------------------


async def test_typeahead_uses_trigram_index(client: httpx.AsyncClient) -> None:
    """Acceptance: EXPLAIN proves name typeahead uses contacts_name_trgm (no Seq Scan)."""
    tok, ws = await _owner(client, "Typeahead")
    for i in range(50):
        await client.post(
            "/v0/contacts/identify",
            json={"external_id": f"u{i}", "name": f"Alice {i}"},
            headers=_auth(tok),
        )

    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
    async with session_scope(ws_uuid) as session:
        # Under FORCED RLS the typeahead is served by the composite GIN index
        # contacts_name_trgm = gin(workspace_id, name gin_trgm_ops): the leakproof
        # workspace_id equality (added by the RLS policy) is the index cond, while ILIKE
        # (non-leakproof) is applied as a filter. With seqscan disabled the planner must
        # choose that index — proving typeahead is index-served, not a scan. (A bare
        # gin(name) index is never usable under RLS because ILIKE is not leakproof.)
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        rows = (
            await session.execute(text("EXPLAIN SELECT id FROM contacts WHERE name ILIKE '%ali%'"))
        ).all()
    plan = "\n".join(r[0] for r in rows)
    assert "contacts_name_trgm" in plan, plan
    assert "Seq Scan" not in plan, plan


# --- cross-tenant isolation (master rule 1) -----------------------------------


async def test_contacts_cross_tenant_isolation(client: httpx.AsyncClient) -> None:
    tok_a, _ = await _owner(client, "AlphaC")
    tok_b, _ = await _owner(client, "BravoC")

    a = (
        await client.post(
            "/v0/contacts/identify",
            json={"external_id": "shared", "name": "A's user"},
            headers=_auth(tok_a),
        )
    ).json()

    # B sees none of A's contacts.
    assert (await client.get("/v0/contacts", headers=_auth(tok_b))).json()["items"] == []
    # B cannot read A's contact (RLS hides it → 404).
    assert (await client.get(f"/v0/contacts/{a['id']}", headers=_auth(tok_b))).status_code == 404
    # B can reuse the same external_id for its own, distinct contact.
    b = (
        await client.post(
            "/v0/contacts/identify",
            json={"external_id": "shared", "name": "B's user"},
            headers=_auth(tok_b),
        )
    ).json()
    assert b["id"] != a["id"]
    assert b["name"] == "B's user"


async def test_contacts_unset_guc_returns_zero_rows(client: httpx.AsyncClient) -> None:
    """The RLS backstop: with no app.ws set, tenant tables return nothing."""
    from relay.core.db import get_sessionmaker
    from relay.modules.crm.models import Contact

    tok, _ = await _owner(client, "HasContacts")
    await client.post("/v0/contacts/identify", json={"external_id": "u1"}, headers=_auth(tok))

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        # Deliberately do NOT set app.ws.
        count = await session.scalar(select(func.count()).select_from(Contact))
    assert count == 0


# --- companies + attribute-definition CRUD ------------------------------------


async def test_company_crud_and_linking(client: httpx.AsyncClient) -> None:
    tok, _ = await _owner(client, "Companies")
    company = (
        await client.post(
            "/v0/companies",
            json={"name": "Globex", "domain": "globex.com", "external_id": "co1"},
            headers=_auth(tok),
        )
    ).json()
    contact = (
        await client.post("/v0/contacts/identify", json={"external_id": "u1"}, headers=_auth(tok))
    ).json()

    link = await client.post(
        f"/v0/contacts/{contact['id']}/companies",
        json={"company_id": company["id"]},
        headers=_auth(tok),
    )
    assert link.status_code == 204
    linked = (
        await client.get(f"/v0/contacts/{contact['id']}/companies", headers=_auth(tok))
    ).json()
    assert [c["id"] for c in linked] == [company["id"]]

    unlink = await client.delete(
        f"/v0/contacts/{contact['id']}/companies/{company['id']}", headers=_auth(tok)
    )
    assert unlink.status_code == 204
    linked = (
        await client.get(f"/v0/contacts/{contact['id']}/companies", headers=_auth(tok))
    ).json()
    assert linked == []
