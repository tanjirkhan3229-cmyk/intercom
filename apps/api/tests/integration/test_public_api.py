"""Public API v0 — API-key auth, scopes, allowlist, rate limiting (P0.11 acceptance)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import uuid4

import httpx
import pytest

pytestmark = pytest.mark.integration

PASSWORD = "password123"


async def _owner(client: httpx.AsyncClient, ws_name: str = "Acme") -> tuple[str, str]:
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


async def _create_key(client: httpx.AsyncClient, token: str, scopes: list[str]) -> dict[str, str]:
    resp = await client.post(
        "/v0/api-keys", json={"name": "test", "scopes": scopes}, headers=_auth(token)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_api_key_read_access_and_jwt_regression(client: httpx.AsyncClient) -> None:
    token, _ = await _owner(client)
    key = (await _create_key(client, token, ["read"]))["key"]

    r = await client.get("/v0/contacts", headers=_auth(key))
    assert r.status_code == 200, r.text
    header_names = {k.lower() for k in r.headers}
    assert "x-ratelimit-limit" in header_names  # rate headers stamped on api-key responses
    assert "x-ratelimit-remaining" in header_names

    # JWT (the agent app) still works and is NOT rate-limited (no rate headers).
    r_jwt = await client.get("/v0/contacts", headers=_auth(token))
    assert r_jwt.status_code == 200
    assert "x-ratelimit-limit" not in {k.lower() for k in r_jwt.headers}


async def test_read_scope_cannot_write(client: httpx.AsyncClient) -> None:
    token, _ = await _owner(client)
    key = (await _create_key(client, token, ["read"]))["key"]
    r = await client.post("/v0/contacts/identify", json={"email": "a@b.com"}, headers=_auth(key))
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "permission_denied"


async def test_write_scope_can_write_and_implies_read(client: httpx.AsyncClient) -> None:
    token, _ = await _owner(client)
    key = (await _create_key(client, token, ["write"]))["key"]
    w = await client.post("/v0/contacts/identify", json={"email": "a@b.com"}, headers=_auth(key))
    assert w.status_code == 200, w.text
    r = await client.get("/v0/contacts", headers=_auth(key))  # write implies read
    assert r.status_code == 200


async def test_allowlist_blocks_admin_routes_for_api_keys(client: httpx.AsyncClient) -> None:
    token, _ = await _owner(client)
    key = (await _create_key(client, token, ["read", "write"]))["key"]

    # Admin routes are not in the API-key allowlist → 403, even with write scope...
    assert (await client.get("/v0/members", headers=_auth(key))).status_code == 403
    assert (await client.get("/v0/api-keys", headers=_auth(key))).status_code == 403
    assert (
        await client.post("/v0/webhook_subscriptions", json={}, headers=_auth(key))
    ).status_code == 403
    # ...but the JWT admin can reach them.
    assert (await client.get("/v0/members", headers=_auth(token))).status_code == 200


async def test_revoked_key_is_rejected(client: httpx.AsyncClient) -> None:
    token, _ = await _owner(client)
    created = await _create_key(client, token, ["read"])
    key, key_id = created["key"], created["id"]
    assert (await client.get("/v0/contacts", headers=_auth(key))).status_code == 200
    assert (await client.delete(f"/v0/api-keys/{key_id}", headers=_auth(token))).status_code == 204
    assert (await client.get("/v0/contacts", headers=_auth(key))).status_code == 401


async def test_malformed_key_rejected(client: httpx.AsyncClient) -> None:
    assert (
        await client.get("/v0/contacts", headers=_auth("relaysk_wrk_bad_secret"))
    ).status_code == 401
    assert (await client.get("/v0/contacts", headers=_auth("not-even-close"))).status_code == 401


async def test_spoofed_workspace_prefix_rejected(client: httpx.AsyncClient) -> None:
    """A real secret with another workspace's prefix must fail (hash mismatch + RLS)."""
    token_a, _ = await _owner(client, "A")
    key_a = (await _create_key(client, token_a, ["read"]))["key"]
    _, ws_b = await _owner(client, "B")
    _, ws_b_b62 = ws_b.split("_", 1)
    parts = key_a.split("_")  # relaysk, wrk, <b62A>, <secret...>
    spoof = "_".join(["relaysk", "wrk", ws_b_b62, *parts[3:]])
    assert (await client.get("/v0/contacts", headers=_auth(spoof))).status_code == 401


@pytest.fixture
def tiny_rate_limit() -> Iterator[None]:
    from relay.settings import get_settings

    overrides = {"PUBLIC_API_RATE_CAPACITY": "1", "PUBLIC_API_RATE_REFILL_PER_SEC": "0"}
    old = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    get_settings.cache_clear()
    yield
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_settings.cache_clear()


async def test_rate_limit_returns_429_with_retry_after(
    client: httpx.AsyncClient, tiny_rate_limit: None
) -> None:
    token, _ = await _owner(client)
    key = (await _create_key(client, token, ["read"]))["key"]

    r1 = await client.get("/v0/contacts", headers=_auth(key))
    assert r1.status_code == 200, r1.text
    assert r1.headers["x-ratelimit-limit"] == "1"

    r2 = await client.get("/v0/contacts", headers=_auth(key))
    assert r2.status_code == 429, r2.text
    assert r2.json()["error"]["code"] == "rate_limited"
    assert int(r2.headers["retry-after"]) >= 1
    assert r2.headers["x-ratelimit-remaining"] == "0"
