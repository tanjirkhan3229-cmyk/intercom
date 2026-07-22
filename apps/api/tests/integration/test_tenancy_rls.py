"""Cross-tenant isolation + the RLS backstop (RFC-002 §7).

Proves zero leakage across the HTTP surface, that an **unset** ``app.ws`` returns zero rows,
and that RLS — not an app-layer WHERE clause — is what scopes queries (our services carry no
explicit ``workspace_id`` filter; RLS is the enforcement).
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from relay.core.db import get_sessionmaker, session_scope
from relay.core.ids import IdPrefix, decode_public_id
from relay.modules.identity.models import Membership, Team

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
    assert resp.status_code == 201
    body = resp.json()
    return body["access_token"], body["workspace"]["id"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_cross_tenant_isolation_over_endpoints(client: httpx.AsyncClient) -> None:
    tok_a, _ws_a = await _owner(client, "Alpha")
    tok_b, _ws_b = await _owner(client, "Bravo")

    # A creates a team; B must not see or touch it.
    created = await client.post("/v0/teams", json={"name": "Support"}, headers=_auth(tok_a))
    assert created.status_code == 201
    team_a = created.json()["id"]

    assert (await client.get("/v0/teams", headers=_auth(tok_b))).json() == []
    assert any(
        t["id"] == team_a for t in (await client.get("/v0/teams", headers=_auth(tok_a))).json()
    )

    # B deleting A's team → 404 (RLS hides the row; it doesn't exist for B).
    assert (await client.delete(f"/v0/teams/{team_a}", headers=_auth(tok_b))).status_code == 404

    # Member lists are per-workspace and disjoint.
    members_a = (await client.get("/v0/members", headers=_auth(tok_a))).json()
    members_b = (await client.get("/v0/members", headers=_auth(tok_b))).json()
    assert len(members_a) == 1 and len(members_b) == 1
    assert members_a[0]["admin"]["id"] != members_b[0]["admin"]["id"]


async def test_unset_guc_returns_zero_rows(client: httpx.AsyncClient) -> None:
    """The fixture the prompt asks for: no app.ws set ⇒ tenant tables return nothing."""
    await _owner(client, "HasData")  # ensure at least one membership exists somewhere

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        # Deliberately DO NOT set app.ws.
        count = await session.scalar(select(func.count()).select_from(Membership))
    assert count == 0


async def test_rls_scopes_queries_without_app_filter(client: httpx.AsyncClient) -> None:
    """Services query `select(Team)` with no workspace filter — RLS alone scopes it."""
    tok_a, _ws_a = await _owner(client, "Foxtrot")
    await client.post("/v0/teams", json={"name": "TeamA"}, headers=_auth(tok_a))

    _tok_b, ws_b = await _owner(client, "Golf")
    await client.post("/v0/teams", json={"name": "TeamB"}, headers=_auth(_tok_b))
    ws_b_uuid = decode_public_id(IdPrefix.WORKSPACE, ws_b)

    # Query under B's GUC only — must never surface A's rows, despite no WHERE clause.
    async with session_scope(ws_b_uuid) as session:
        teams = (await session.scalars(select(Team))).all()
    assert len(teams) >= 1
    assert all(t.workspace_id == ws_b_uuid for t in teams)
    assert all(t.name != "TeamA" for t in teams)
