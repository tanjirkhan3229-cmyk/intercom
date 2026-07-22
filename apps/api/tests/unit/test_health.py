"""Unit tests for the hello-world + liveness endpoints (P0.0 CI target).

Uses httpx ASGITransport so no server/DB is required — /healthz has no dependencies.
"""

from __future__ import annotations

import httpx
import pytest

from relay.main import create_app


@pytest.fixture
def client() -> httpx.AsyncClient:
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_healthz(client: httpx.AsyncClient) -> None:
    async with client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_hello_world(client: httpx.AsyncClient) -> None:
    async with client:
        resp = await client.get("/v0/hello")
    assert resp.status_code == 200
    body = resp.json()
    assert body["message"] == "Hello from Relay"
    assert body["service"] == "relay-api"


async def test_request_id_echoed(client: httpx.AsyncClient) -> None:
    async with client:
        resp = await client.get("/healthz", headers={"X-Request-ID": "abc123"})
    assert resp.headers["X-Request-ID"] == "abc123"
