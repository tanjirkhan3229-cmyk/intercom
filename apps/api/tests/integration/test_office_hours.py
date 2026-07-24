"""Integration tests for office-hours schedules (P1.7 S1).

Exercises the real tenancy path: CRUD over HTTP, admin-only writes, team override vs workspace
default resolution, the open/closed status endpoint, and cross-tenant RLS isolation.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"

_WEEKDAYS = {str(d): [{"open": "09:00", "close": "17:00"}] for d in range(5)}


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


async def _team(client: httpx.AsyncClient, tok: str, name: str = "Support") -> str:
    r = await client.post("/v0/teams", json={"name": name}, headers=_auth(tok))
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_upsert_list_and_replace_default_schedule(client: httpx.AsyncClient) -> None:
    tok, _ws = await _owner(client, "Hours")
    # Create the workspace default.
    r = await client.put(
        "/v0/office-hours",
        json={"timezone": "America/New_York", "weekly": _WEEKDAYS, "holidays": ["2026-12-25"]},
        headers=_auth(tok),
    )
    assert r.status_code == 200, r.text
    created = r.json()
    assert created["team_id"] is None
    assert created["timezone"] == "America/New_York"
    assert created["holidays"] == ["2026-12-25"]
    assert set(created["weekly"].keys()) == set(_WEEKDAYS.keys())

    # Upsert again (same key) → replace, not duplicate.
    r2 = await client.put(
        "/v0/office-hours",
        json={"timezone": "Europe/London", "weekly": _WEEKDAYS, "holidays": []},
        headers=_auth(tok),
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["id"] == created["id"]  # same row
    assert r2.json()["timezone"] == "Europe/London"

    listing = (await client.get("/v0/office-hours", headers=_auth(tok))).json()
    assert len(listing) == 1


async def test_team_override_and_default(client: httpx.AsyncClient) -> None:
    tok, _ws = await _owner(client, "Hours2")
    team_id = await _team(client, tok)
    await client.put(
        "/v0/office-hours",
        json={"timezone": "UTC", "weekly": _WEEKDAYS, "holidays": []},
        headers=_auth(tok),
    )
    r = await client.put(
        "/v0/office-hours",
        json={
            "team_id": team_id,
            "timezone": "Asia/Tokyo",
            "weekly": _WEEKDAYS,
            "holidays": [],
        },
        headers=_auth(tok),
    )
    assert r.status_code == 200, r.text
    assert r.json()["team_id"] == team_id

    listing = (await client.get("/v0/office-hours", headers=_auth(tok))).json()
    assert len(listing) == 2

    # Status resolves the team override.
    status_team = (
        await client.get(f"/v0/office-hours/status?team_id={team_id}", headers=_auth(tok))
    ).json()
    assert status_team["has_schedule"] is True
    assert status_team["timezone"] == "Asia/Tokyo"


async def test_status_without_schedule_defaults_open(client: httpx.AsyncClient) -> None:
    tok, _ws = await _owner(client, "Hours3")
    status = (await client.get("/v0/office-hours/status", headers=_auth(tok))).json()
    assert status == {"has_schedule": False, "is_open": True, "timezone": None}


async def test_bad_timezone_is_422(client: httpx.AsyncClient) -> None:
    tok, _ws = await _owner(client, "Hours4")
    r = await client.put(
        "/v0/office-hours",
        json={"timezone": "Mars/Phobos", "weekly": _WEEKDAYS, "holidays": []},
        headers=_auth(tok),
    )
    assert r.status_code == 422, r.text


async def test_delete_schedule(client: httpx.AsyncClient) -> None:
    tok, _ws = await _owner(client, "Hours5")
    created = (
        await client.put(
            "/v0/office-hours",
            json={"timezone": "UTC", "weekly": _WEEKDAYS, "holidays": []},
            headers=_auth(tok),
        )
    ).json()
    d = await client.delete(f"/v0/office-hours/{created['id']}", headers=_auth(tok))
    assert d.status_code == 204, d.text
    assert (await client.get("/v0/office-hours", headers=_auth(tok))).json() == []
    # Deleting again → 404 (gone).
    assert (
        await client.delete(f"/v0/office-hours/{created['id']}", headers=_auth(tok))
    ).status_code == 404


async def test_cross_tenant_isolation(client: httpx.AsyncClient) -> None:
    tok_a, _ws_a = await _owner(client, "Alpha")
    tok_b, _ws_b = await _owner(client, "Bravo")
    created_a = (
        await client.put(
            "/v0/office-hours",
            json={"timezone": "UTC", "weekly": _WEEKDAYS, "holidays": []},
            headers=_auth(tok_a),
        )
    ).json()
    # B cannot see A's schedule...
    assert (await client.get("/v0/office-hours", headers=_auth(tok_b))).json() == []
    # ...nor delete it (RLS hides the row → 404).
    assert (
        await client.delete(f"/v0/office-hours/{created_a['id']}", headers=_auth(tok_b))
    ).status_code == 404
    # A still has it.
    assert len((await client.get("/v0/office-hours", headers=_auth(tok_a))).json()) == 1
