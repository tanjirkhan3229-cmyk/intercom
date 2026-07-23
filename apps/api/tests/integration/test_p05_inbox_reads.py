"""Integration tests for the read APIs P0.5 (Inbox app v1) adds on top of P0.3 messaging.

These back the contact side panel + the "Unassigned" view:
- ``GET /conversations?unassigned=true`` returns only conversations with no assignee;
- ``GET /contacts/{id}/conversations`` returns a contact's conversations across all states,
  newest-activity-first, keyset-paginated;
- ``GET /contacts/{id}/events`` returns recent tracked events for a contact;
- all three remain workspace-isolated (RLS): another tenant sees none of it.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

pytestmark = pytest.mark.integration

PASSWORD = "password123"


async def _owner(client: httpx.AsyncClient, ws_name: str) -> str:
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
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _me_admin_id(client: httpx.AsyncClient, tok: str) -> str:
    return (await client.get("/v0/auth/me", headers=_auth(tok))).json()["admin"]["id"]


async def _contact(client: httpx.AsyncClient, tok: str) -> str:
    r = await client.post(
        "/v0/contacts/identify", json={"external_id": uuid4().hex}, headers=_auth(tok)
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _conversation(client: httpx.AsyncClient, tok: str, contact_id: str) -> dict:
    r = await client.post(
        "/v0/conversations",
        json={"contact_id": contact_id, "body": "hi"},
        headers=_auth(tok),
    )
    assert r.status_code == 201, r.text
    return r.json()


# --- Unassigned view ----------------------------------------------------------


async def test_unassigned_filter_excludes_assigned(client: httpx.AsyncClient) -> None:
    tok = await _owner(client, "Unassigned")
    me = await _me_admin_id(client, tok)
    assigned = await _conversation(client, tok, await _contact(client, tok))
    unassigned = await _conversation(client, tok, await _contact(client, tok))

    r = await client.post(
        f"/v0/conversations/{assigned['id']}/assign",
        json={"assignee_id": me},
        headers=_auth(tok),
    )
    assert r.status_code == 200, r.text

    page = (
        await client.get("/v0/conversations", params={"unassigned": "true"}, headers=_auth(tok))
    ).json()
    ids = {c["id"] for c in page["items"]}
    assert unassigned["id"] in ids
    assert assigned["id"] not in ids
    assert all(c["assignee_id"] is None for c in page["items"])


# --- Contact side panel: conversations ----------------------------------------


async def test_contact_conversations_all_states_newest_first(client: httpx.AsyncClient) -> None:
    tok = await _owner(client, "PanelConvs")
    contact_id = await _contact(client, tok)
    first = await _conversation(client, tok, contact_id)
    second = await _conversation(client, tok, contact_id)

    # Close the first — the panel must still show it (all states, not just open).
    closed = await client.post(
        f"/v0/conversations/{first['id']}/state", json={"state": "closed"}, headers=_auth(tok)
    )
    assert closed.status_code == 200, closed.text

    page = (
        await client.get(f"/v0/contacts/{contact_id}/conversations", headers=_auth(tok))
    ).json()
    ids = [c["id"] for c in page["items"]]
    assert set(ids) == {first["id"], second["id"]}
    # newest activity first: the just-closed `first` has the most recent last_part_at.
    assert ids[0] == first["id"]


# --- Contact side panel: events -----------------------------------------------


async def test_contact_events_read_back(client: httpx.AsyncClient) -> None:
    from relay.modules.crm.tasks import drain_events

    tok = await _owner(client, "PanelEvents")
    contact_id = await _contact(client, tok)
    r = await client.post(
        "/v0/events/track",
        json={
            "events": [
                {"contact_id": contact_id, "name": "signed_up", "properties": {"plan": "pro"}}
            ]
        },
        headers=_auth(tok),
    )
    assert r.status_code == 202, r.text
    drain_events()  # synchronous drain: Redis buffer → COPY → partitioned events table

    events = (
        await client.get(f"/v0/contacts/{contact_id}/events", headers=_auth(tok))
    ).json()
    assert any(e["name"] == "signed_up" and e["properties"].get("plan") == "pro" for e in events)


# --- Cross-tenant isolation ---------------------------------------------------


async def test_contact_reads_are_workspace_isolated(client: httpx.AsyncClient) -> None:
    tok_a = await _owner(client, "TenantA")
    tok_b = await _owner(client, "TenantB")
    contact_a = await _contact(client, tok_a)
    await _conversation(client, tok_a, contact_a)

    # Tenant B asking for tenant A's contact must 404 (RLS-scoped), never leak.
    r = await client.get(f"/v0/contacts/{contact_a}/conversations", headers=_auth(tok_b))
    assert r.status_code == 404, r.text
    r2 = await client.get(f"/v0/contacts/{contact_a}/events", headers=_auth(tok_b))
    assert r2.status_code == 404, r2.text
