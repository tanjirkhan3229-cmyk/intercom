"""Zapier integration tests (P1.9): auth test + REST-hook subscribe/unsubscribe.

Zapier triggers reuse the webhooks delivery pipeline — a subscribe creates a real
``webhook_subscription`` row, an unsubscribe deletes it. SSRF validation is core.ssrf's concern
(tested there), so it is mocked here to keep the test offline. Auth uses the owner JWT (JWT
principals aren't restricted by the API-key allowlist); the allowlist contract itself is asserted
in the unit test ``test_slack_format.test_zapier_routes_are_api_key_allowlisted``.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from relay.core.db import session_scope
from relay.core.ids import IdPrefix, decode_public_id
from relay.modules.integrations import service as integ_service
from relay.modules.webhooks.models import WebhookSubscription

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


async def test_zapier_auth_test(client: httpx.AsyncClient) -> None:
    tok, ws_pub = await _owner(client, "ZapAuth")
    resp = await client.get("/v0/zapier/auth/test", headers=_auth(tok))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["workspace_id"] == ws_pub


async def test_zapier_subscribe_creates_and_removes_hook(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tok, ws_pub = await _owner(client, "ZapHooks")
    ws = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    # SSRF check is core.ssrf's job (tested there); keep this test offline.
    monkeypatch.setattr(integ_service, "validate_target", lambda url, *, allow_private: None)

    created = await client.post(
        "/v0/zapier/subscriptions",
        json={"topic": "contact.created", "target_url": "https://hooks.zapier.com/abc"},
        headers=_auth(tok),
    )
    assert created.status_code == 201, created.text
    sub_pub = created.json()["id"]
    assert created.json()["topic"] == "contact.created"

    # A real webhook_subscription now backs the Zapier trigger (reuses the delivery pipeline).
    async with session_scope(ws) as s:
        sub = (await s.scalars(select(WebhookSubscription))).one()
    assert sub.topics == ["contact.created"]
    assert sub.url == "https://hooks.zapier.com/abc"

    # Unsubscribe deletes it.
    deleted = await client.delete(f"/v0/zapier/subscriptions/{sub_pub}", headers=_auth(tok))
    assert deleted.status_code == 204
    async with session_scope(ws) as s:
        assert (await s.scalar(select(func.count()).select_from(WebhookSubscription))) == 0


async def test_zapier_subscribe_rejects_unknown_topic(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tok, _ws = await _owner(client, "ZapBadTopic")
    monkeypatch.setattr(integ_service, "validate_target", lambda url, *, allow_private: None)
    resp = await client.post(
        "/v0/zapier/subscriptions",
        json={"topic": "not.a.topic", "target_url": "https://hooks.zapier.com/x"},
        headers=_auth(tok),
    )
    assert resp.status_code == 422, resp.text
