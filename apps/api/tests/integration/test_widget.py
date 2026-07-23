"""Messenger widget BFF integration tests (P0.6 acceptance, RFC-000 §2.1, RFC-001 §6.3, §10).

Covers the acceptance bar the widget owns on the server side:
- **HMAC mismatch is rejected** (identity verification): a bad/absent ``user_hash`` → 403, the
  correct HMAC → a verified ``user`` contact.
- **A lead's cookie session survives reload**: boot with no identity sets an httpOnly cookie;
  a second boot carrying that cookie resumes the *same* lead.
- End-to-end contact flow (start → thread → idempotent reply → list → rating → realtime token).
- Isolation: a contact can only ever touch its own conversation; the two token audiences
  (agent access vs widget session) don't cross.

The client uses an ``https`` base URL because the cross-site session cookie is ``Secure`` (as it
must be in a real third-party iframe); an ``http`` client would never store/resend it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest

from relay.core.security import compute_identity_hash

pytestmark = pytest.mark.integration

PASSWORD = "password123"


@pytest.fixture
async def widget_client(app_instance) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app_instance)
    async with httpx.AsyncClient(transport=transport, base_url="https://widget.test") as c:
        yield c


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _owner(client: httpx.AsyncClient) -> tuple[str, str]:
    """Sign up an owner; return (agent_access_token, workspace_public_id == widget app_id)."""
    resp = await client.post(
        "/v0/auth/signup",
        json={
            "workspace_name": f"ws-{uuid4().hex}",
            "email": f"owner-{uuid4().hex}@example.com",
            "password": PASSWORD,
            "name": "Owner",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["access_token"], body["workspace"]["id"]


async def _enable_identity_verification(client: httpx.AsyncClient, tok: str, secret: str) -> None:
    resp = await client.patch(
        "/v0/workspace",
        json={
            "settings": {
                "messenger": {"identity_verification": {"enabled": True, "secret": secret}}
            }
        },
        headers=_auth(tok),
    )
    assert resp.status_code == 200, resp.text


# --- Identity verification (HMAC) ---------------------------------------------


async def test_hmac_mismatch_rejected_and_match_accepted(widget_client: httpx.AsyncClient) -> None:
    tok, app_id = await _owner(widget_client)
    secret = "per-workspace-identity-secret"
    await _enable_identity_verification(widget_client, tok, secret)

    # Wrong hash → rejected.
    bad = await widget_client.post(
        "/v0/widget/boot",
        json={"app_id": app_id, "user": {"external_id": "user-1"}, "user_hash": "deadbeef"},
    )
    assert bad.status_code == 403, bad.text

    # Missing hash while verification is on → rejected.
    missing = await widget_client.post(
        "/v0/widget/boot", json={"app_id": app_id, "user": {"external_id": "user-1"}}
    )
    assert missing.status_code == 403, missing.text

    # Correct HMAC → accepted as a verified user.
    good = await widget_client.post(
        "/v0/widget/boot",
        json={
            "app_id": app_id,
            "user": {"external_id": "user-1", "name": "Ada"},
            "user_hash": compute_identity_hash(secret, "user-1"),
        },
    )
    assert good.status_code == 200, good.text
    body = good.json()
    assert body["contact"]["kind"] == "user"
    assert body["session_token"]
    assert body["config"]["identity_verification_enabled"] is True

    # Same verified user booting again resolves to the same contact (idempotent upsert).
    again = await widget_client.post(
        "/v0/widget/boot",
        json={
            "app_id": app_id,
            "user": {"external_id": "user-1"},
            "user_hash": compute_identity_hash(secret, "user-1"),
        },
    )
    assert again.json()["contact"]["id"] == body["contact"]["id"]


async def test_unknown_app_id_is_404(widget_client: httpx.AsyncClient) -> None:
    resp = await widget_client.post(
        "/v0/widget/boot", json={"app_id": "wrk_000000000000000000000000"}
    )
    assert resp.status_code in (404, 422), resp.text


# --- Cookie-scoped lead survives reload ---------------------------------------


async def test_lead_cookie_session_survives_reload(widget_client: httpx.AsyncClient) -> None:
    _, app_id = await _owner(widget_client)

    first = await widget_client.post("/v0/widget/boot", json={"app_id": app_id})
    assert first.status_code == 200, first.text
    b1 = first.json()
    assert b1["contact"]["kind"] == "lead"
    assert "relay_widget" in widget_client.cookies  # session cookie was set

    # "Reload": the browser resends the cookie; the same lead is resumed (no new contact).
    second = await widget_client.post("/v0/widget/boot", json={"app_id": app_id})
    assert second.status_code == 200, second.text
    assert second.json()["contact"]["id"] == b1["contact"]["id"]


async def test_no_cookie_creates_distinct_leads(app_instance) -> None:
    transport = httpx.ASGITransport(app=app_instance)
    async with (
        httpx.AsyncClient(transport=transport, base_url="https://widget.test") as ca,
        httpx.AsyncClient(transport=transport, base_url="https://widget.test") as cb,
    ):
        _, app_id = await _owner(ca)
        a = (await ca.post("/v0/widget/boot", json={"app_id": app_id})).json()
        b = (await cb.post("/v0/widget/boot", json={"app_id": app_id})).json()
        assert a["contact"]["id"] != b["contact"]["id"]


# --- Contact conversation flow ------------------------------------------------


async def test_widget_conversation_flow(widget_client: httpx.AsyncClient) -> None:
    _, app_id = await _owner(widget_client)
    boot = (await widget_client.post("/v0/widget/boot", json={"app_id": app_id})).json()
    h = _auth(boot["session_token"])

    conv = await widget_client.post(
        "/v0/widget/conversations", json={"body": "hello there"}, headers=h
    )
    assert conv.status_code == 201, conv.text
    conv_id = conv.json()["id"]

    parts = (await widget_client.get(f"/v0/widget/conversations/{conv_id}/parts", headers=h)).json()
    assert any(p["author_kind"] == "contact" and p["body"] == "hello there" for p in parts["items"])

    # Idempotent reply: same key → same part, exactly one row.
    key = uuid4().hex
    idem = {**h, "Idempotency-Key": key}
    r1 = await widget_client.post(
        f"/v0/widget/conversations/{conv_id}/reply", json={"body": "more"}, headers=idem
    )
    r2 = await widget_client.post(
        f"/v0/widget/conversations/{conv_id}/reply", json={"body": "more"}, headers=idem
    )
    assert r1.status_code == 201 and r2.status_code == 201, (r1.text, r2.text)
    assert r1.json()["id"] == r2.json()["id"]

    mine = (await widget_client.get("/v0/widget/conversations", headers=h)).json()
    assert any(c["id"] == conv_id for c in mine["items"])

    rating = await widget_client.post(
        f"/v0/widget/conversations/{conv_id}/rating",
        json={"rating": 5, "remark": "great"},
        headers=h,
    )
    assert rating.status_code == 201, rating.text
    assert rating.json()["meta"]["rating"] == 5

    rt = await widget_client.post(f"/v0/widget/conversations/{conv_id}/realtime-token", headers=h)
    assert rt.status_code == 200, rt.text
    assert rt.json()["token"] and rt.json()["ws_url"]


async def test_contact_cannot_touch_another_contacts_conversation(app_instance) -> None:
    transport = httpx.ASGITransport(app=app_instance)
    async with (
        httpx.AsyncClient(transport=transport, base_url="https://widget.test") as ca,
        httpx.AsyncClient(transport=transport, base_url="https://widget.test") as cb,
    ):
        _, app_id = await _owner(ca)
        boot_a = (await ca.post("/v0/widget/boot", json={"app_id": app_id})).json()
        boot_b = (await cb.post("/v0/widget/boot", json={"app_id": app_id})).json()
        ha = _auth(boot_a["session_token"])
        hb = _auth(boot_b["session_token"])

        conv_id = (
            await ca.post("/v0/widget/conversations", json={"body": "secret"}, headers=ha)
        ).json()["id"]

        # Same workspace, different contact: the ownership guard 404s (beyond RLS).
        assert (
            await cb.get(f"/v0/widget/conversations/{conv_id}/parts", headers=hb)
        ).status_code == 404
        assert (
            await cb.post(
                f"/v0/widget/conversations/{conv_id}/reply", json={"body": "sneaky"}, headers=hb
            )
        ).status_code == 404


async def test_token_audiences_do_not_cross(widget_client: httpx.AsyncClient) -> None:
    agent_tok, app_id = await _owner(widget_client)
    widget_tok = (await widget_client.post("/v0/widget/boot", json={"app_id": app_id})).json()[
        "session_token"
    ]

    # A widget session token cannot reach an agent route.
    assert (
        await widget_client.get("/v0/conversations", headers=_auth(widget_tok))
    ).status_code == 401
    # An agent access token cannot reach a widget route.
    assert (
        await widget_client.get("/v0/widget/conversations", headers=_auth(agent_tok))
    ).status_code == 401
