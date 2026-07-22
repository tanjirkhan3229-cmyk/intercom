"""Auth flow integration tests: signup, me, login, refresh rotation + reuse detection, logout."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

pytestmark = pytest.mark.integration

PASSWORD = "password123"


async def _signup(client: httpx.AsyncClient, ws_name: str) -> httpx.Response:
    email = f"owner-{uuid4().hex}@example.com"
    return await client.post(
        "/v0/auth/signup",
        json={"workspace_name": ws_name, "email": email, "password": PASSWORD, "name": "Owner"},
    )


async def _refresh(client: httpx.AsyncClient, token_value: str) -> httpx.Response:
    client.cookies.clear()  # avoid jar interference; present exactly this token
    return await client.post("/v0/auth/refresh", cookies={"relay_rt": token_value})


async def test_signup_sets_session_and_me_works(client: httpx.AsyncClient) -> None:
    resp = await _signup(client, "Acme")
    assert resp.status_code == 201
    body = resp.json()
    assert body["access_token"]
    assert body["role"] == "owner"
    assert body["workspace"]["id"].startswith("wrk_")
    assert body["admin"]["id"].startswith("adm_")
    assert "relay_rt" in resp.cookies

    me = await client.get(
        "/v0/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["workspace"]["id"] == body["workspace"]["id"]


async def test_me_requires_auth(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v0/auth/me")).status_code == 401


async def test_duplicate_email_conflicts(client: httpx.AsyncClient) -> None:
    email = f"dup-{uuid4().hex}@example.com"
    payload = {"workspace_name": "One", "email": email, "password": PASSWORD, "name": "A"}
    assert (await client.post("/v0/auth/signup", json=payload)).status_code == 201
    assert (await client.post("/v0/auth/signup", json=payload)).status_code == 409


async def test_login_after_signup(client: httpx.AsyncClient) -> None:
    email = f"login-{uuid4().hex}@example.com"
    await client.post(
        "/v0/auth/signup",
        json={"workspace_name": "LoginCo", "email": email, "password": PASSWORD, "name": "L"},
    )
    ok = await client.post("/v0/auth/login", json={"email": email, "password": PASSWORD})
    assert ok.status_code == 200
    assert ok.json()["role"] == "owner"

    bad = await client.post("/v0/auth/login", json={"email": email, "password": "nope"})
    assert bad.status_code == 401


async def test_refresh_rotation_and_reuse_detection(client: httpx.AsyncClient) -> None:
    resp = await _signup(client, "RotateCo")
    rt1 = resp.cookies["relay_rt"]

    r2 = await _refresh(client, rt1)
    assert r2.status_code == 200
    rt2 = r2.cookies["relay_rt"]
    assert rt2 != rt1  # rotated

    r3 = await _refresh(client, rt2)
    assert r3.status_code == 200
    rt3 = r3.cookies["relay_rt"]

    # Replaying the retired rt1 → reuse detected → 401 and the whole family is revoked.
    reuse = await _refresh(client, rt1)
    assert reuse.status_code == 401

    # Because the family was revoked, the latest (rt3) is now dead too.
    after = await _refresh(client, rt3)
    assert after.status_code == 401


async def test_logout_revokes_refresh(client: httpx.AsyncClient) -> None:
    resp = await _signup(client, "LogoutCo")
    rt = resp.cookies["relay_rt"]

    client.cookies.clear()
    out = await client.post("/v0/auth/logout", cookies={"relay_rt": rt})
    assert out.status_code == 204

    assert (await _refresh(client, rt)).status_code == 401
