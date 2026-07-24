"""Integration tests for balanced (load-aware) assignment + availability (P1.7 S4).

Covers the headline acceptance — a conversation burst distributes within ±1 of ideal across a team
— plus away/capacity exclusion, the all-unavailable conflict, and availability CRUD (self + admin).
A multi-agent team is seeded directly in the DB (the invite/accept HTTP flow is out of scope here).
"""

from __future__ import annotations

from collections import Counter
from uuid import uuid4

import httpx
import pytest

from relay.core.db import session_scope
from relay.core.ids import IdPrefix, decode_public_id, encode_public_id
from relay.modules.identity.models import Admin, Membership, TeamMembership

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


async def _seed_team(
    client: httpx.AsyncClient, tok: str, ws_pub: str, n: int
) -> tuple[str, list[str]]:
    """Create a team with ``n`` assignable agents; return (team public id, [admin public ids])."""
    team = (await client.post("/v0/teams", json={"name": "Support"}, headers=_auth(tok))).json()
    team_uuid = decode_public_id(IdPrefix.TEAM, team["id"])
    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    admin_pubs: list[str] = []
    async with session_scope(ws_uuid) as session:
        for i in range(n):
            admin = Admin(email=f"agent-{uuid4().hex}@example.com", name=f"Agent{i}")
            session.add(admin)
            await session.flush()
            membership = Membership(workspace_id=ws_uuid, admin_id=admin.id, role="agent")
            session.add(membership)
            await session.flush()
            session.add(
                TeamMembership(workspace_id=ws_uuid, team_id=team_uuid, membership_id=membership.id)
            )
            admin_pubs.append(encode_public_id(IdPrefix.ADMIN, admin.id))
    return team["id"], admin_pubs


async def _unassigned_conv(client: httpx.AsyncClient, tok: str) -> dict:
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


async def _balance(
    client: httpx.AsyncClient, tok: str, conv_id: str, team_id: str
) -> httpx.Response:
    return await client.post(
        f"/v0/conversations/{conv_id}/assign/balanced",
        json={"team_id": team_id},
        headers=_auth(tok),
    )


# --- fairness -----------------------------------------------------------------


async def test_burst_distributes_within_one_of_ideal(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client, "Balance")
    team_id, admins = await _seed_team(client, tok, ws, 4)

    assigned: list[str] = []
    for _ in range(12):
        conv = await _unassigned_conv(client, tok)
        r = await _balance(client, tok, conv["id"], team_id)
        assert r.status_code == 200, r.text
        assigned.append(r.json()["assignee_id"])

    counts = Counter(assigned)
    assert set(counts) <= set(admins)  # only team agents
    assert len(counts) == 4  # every agent used
    assert max(counts.values()) - min(counts.values()) <= 1  # ±1 of ideal (here exactly 3 each)
    assert sum(counts.values()) == 12


async def test_empty_team_is_422(client: httpx.AsyncClient) -> None:
    tok, _ws = await _owner(client, "BalanceEmpty")
    team = (await client.post("/v0/teams", json={"name": "Empty"}, headers=_auth(tok))).json()
    conv = await _unassigned_conv(client, tok)
    r = await _balance(client, tok, conv["id"], team["id"])
    assert r.status_code == 422, r.text


# --- away / capacity ----------------------------------------------------------


async def test_away_agent_is_skipped(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client, "BalanceAway")
    team_id, admins = await _seed_team(client, tok, ws, 3)
    away = await client.put(
        f"/v0/availability/{admins[0]}", json={"away": True}, headers=_auth(tok)
    )
    assert away.status_code == 200, away.text

    seen: set[str] = set()
    for _ in range(9):
        conv = await _unassigned_conv(client, tok)
        r = await _balance(client, tok, conv["id"], team_id)
        seen.add(r.json()["assignee_id"])
    assert admins[0] not in seen
    assert seen == set(admins[1:])


async def test_capacity_cap_excludes_at_limit(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client, "BalanceCap")
    team_id, admins = await _seed_team(client, tok, ws, 2)
    await client.put(f"/v0/availability/{admins[0]}", json={"max_open": 1}, headers=_auth(tok))

    assigned: list[str] = []
    for _ in range(5):
        conv = await _unassigned_conv(client, tok)
        r = await _balance(client, tok, conv["id"], team_id)
        assigned.append(r.json()["assignee_id"])
    counts = Counter(assigned)
    assert counts.get(admins[0], 0) <= 1  # capped at one open conversation
    assert counts[admins[1]] == 5 - counts.get(admins[0], 0)


async def test_all_unavailable_conflicts(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client, "BalanceFull")
    team_id, admins = await _seed_team(client, tok, ws, 2)
    for a in admins:
        await client.put(f"/v0/availability/{a}", json={"max_open": 0}, headers=_auth(tok))
    conv = await _unassigned_conv(client, tok)
    r = await _balance(client, tok, conv["id"], team_id)
    assert r.status_code == 409, r.text


# --- availability CRUD --------------------------------------------------------


async def test_my_availability_roundtrip(client: httpx.AsyncClient) -> None:
    tok, _ws = await _owner(client, "BalanceMe")
    default = (await client.get("/v0/me/availability", headers=_auth(tok))).json()
    assert default["away"] is False
    assert default["max_open"] is None

    upd = await client.put(
        "/v0/me/availability", json={"away": True, "max_open": 5}, headers=_auth(tok)
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["away"] is True
    assert upd.json()["max_open"] == 5

    got = (await client.get("/v0/me/availability", headers=_auth(tok))).json()
    assert got["away"] is True
    assert got["max_open"] == 5
