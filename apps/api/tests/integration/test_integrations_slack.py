"""Slack integration tests (P1.9) — the reply-from-Slack round-trip is the acceptance.

Outbound HTTP to Slack is mocked (the logic under test is threading + signature + mapping, not
httpx). Covers: connect (secrets encrypted), outbound notify creates a thread map, a SIGNED inbound
reply lands as an admin part in the mapped conversation (the round-trip), bad signature → 403, the
url_verification handshake, and cross-tenant team resolution.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from relay.core.db import session_scope
from relay.core.ids import IdPrefix, decode_public_id
from relay.modules.integrations import service as integ_service
from relay.modules.integrations import slack_sign
from relay.modules.integrations.models import IntegrationAccount, SlackThreadMap
from relay.modules.messaging.models import ConversationPart

pytestmark = pytest.mark.integration

PASSWORD = "password123"
CHANNEL_ID = "C0SUPPORT"
SIGNING_SECRET = "8f742231b10c8538a055a3ee6ed7a9d5"
ROOT_TS = "1699999999.000100"


def _new_team_id() -> str:
    # A Slack team_id is globally unique (one Slack workspace ↔ one Relay workspace), so each test
    # uses a fresh one — the test DB persists rows across tests.
    return f"T{uuid4().hex[:10].upper()}"


class _FakeResp:
    status_code = 200

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def json(self) -> dict[str, Any]:
        return self._data


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


async def _connect_slack(client: httpx.AsyncClient, tok: str, team_id: str) -> str:
    resp = await client.post(
        "/v0/integrations/slack",
        json={
            "team_id": team_id,
            "team_name": "Acme",
            "channel_id": CHANNEL_ID,
            "channel_name": "#support",
            "bot_token": "xoxb-fake-bot-token",
            "signing_secret": SIGNING_SECRET,
        },
        headers=_auth(tok),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _open_conversation(client: httpx.AsyncClient, tok: str) -> str:
    contact = (
        await client.post(
            "/v0/contacts/identify", json={"external_id": uuid4().hex}, headers=_auth(tok)
        )
    ).json()
    conv = (
        await client.post(
            "/v0/conversations",
            json={"contact_id": contact["id"], "body": "I need help"},
            headers=_auth(tok),
        )
    ).json()
    return str(conv["id"])


async def test_connect_encrypts_secrets(client: httpx.AsyncClient) -> None:
    tok, ws_pub = await _owner(client, "SlackConnect")
    team = _new_team_id()
    await _connect_slack(client, tok, team)
    ws = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    async with session_scope(ws) as s:
        acc = (await s.scalars(select(IntegrationAccount))).one()
    # Plaintext secrets never persisted.
    assert "xoxb-fake-bot-token" not in json.dumps(acc.config)
    assert SIGNING_SECRET not in json.dumps(acc.config)
    assert acc.config["team_id"] == team
    listed = await client.get("/v0/integrations", headers=_auth(tok))
    assert listed.json()[0]["channel_id"] == CHANNEL_ID


async def test_slack_round_trip(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Outbound notify → signed inbound reply → admin part in the conversation."""
    tok, ws_pub = await _owner(client, "SlackRT")
    ws = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    team = _new_team_id()
    await _connect_slack(client, tok, team)
    conv_pub = await _open_conversation(client, tok)
    conv_id = decode_public_id(IdPrefix.CONVERSATION, conv_pub)

    # --- outbound: mock the Slack API, deliver, assert the thread map is created ---
    posts: list[tuple[str, bytes]] = []

    def _fake_post(url: str, *, content: bytes, headers: dict, timeout: float, allow_private: bool):
        posts.append((url, content))
        return _FakeResp({"ok": True, "ts": ROOT_TS})

    monkeypatch.setattr(integ_service, "guarded_post", _fake_post)
    result = await integ_service.deliver_slack_notification(
        ws, conv_pub, "conversation.created", "New conversation"
    )
    assert result == "delivered"
    assert posts and posts[0][0].endswith("/chat.postMessage")
    async with session_scope(ws) as s:
        tmap = (await s.scalars(select(SlackThreadMap))).one()
    assert tmap.thread_ts == ROOT_TS and tmap.channel_id == CHANNEL_ID

    # --- inbound: a SIGNED Slack thread reply hits the events endpoint (fast-ack) ---
    captured: list[tuple[str, list]] = []
    from relay.worker import celery_app

    monkeypatch.setattr(
        celery_app, "send_task", lambda name, args, **kw: captured.append((name, args))
    )

    event = {
        "type": "event_callback",
        "team_id": team,
        "event_id": "Ev123",
        "event": {
            "type": "message",
            "channel": CHANNEL_ID,
            "thread_ts": ROOT_TS,
            "text": "Sure, happy to help!",
            "user": "U0AGENT",
        },
    }
    raw = json.dumps(event).encode("utf-8")
    ts = int(dt.datetime.now(dt.UTC).timestamp())
    headers = {
        slack_sign.SIGNATURE_HEADER: slack_sign.compute_signature(SIGNING_SECRET, ts, raw),
        slack_sign.TIMESTAMP_HEADER: str(ts),
        "Content-Type": "application/json",
    }
    resp = await client.post("/v0/integrations/slack/events", content=raw, headers=headers)
    assert resp.status_code == 200, resp.text
    assert captured and captured[0][0] == "integrations.slack_ingest_inbound"

    # --- run the enqueued ingest (no worker in tests) → the reply lands as an admin part ---
    assert await integ_service.ingest_slack_event(ws, raw.decode("utf-8")) == "posted"
    async with session_scope(ws) as s:
        parts = list(
            (
                await s.scalars(
                    select(ConversationPart).where(
                        ConversationPart.conversation_id == conv_id,
                        ConversationPart.author_kind == "admin",
                        ConversationPart.part_type == "comment",
                    )
                )
            ).all()
        )
    assert len(parts) == 1
    assert parts[0].body == "Sure, happy to help!"
    assert parts[0].channel_meta.get("source") == "slack"

    # Idempotent: replaying the same Slack event (same event_id) does not duplicate the reply.
    assert await integ_service.ingest_slack_event(ws, raw.decode("utf-8")) == "duplicate"


async def test_inbound_bad_signature_rejected(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tok, _ws = await _owner(client, "SlackBadSig")
    team = _new_team_id()
    await _connect_slack(client, tok, team)
    from relay.worker import celery_app

    monkeypatch.setattr(celery_app, "send_task", lambda *a, **k: None)

    raw = json.dumps({"type": "event_callback", "team_id": team, "event": {}}).encode()
    ts = int(dt.datetime.now(dt.UTC).timestamp())
    headers = {
        slack_sign.SIGNATURE_HEADER: "v0=deadbeef",  # wrong
        slack_sign.TIMESTAMP_HEADER: str(ts),
        "Content-Type": "application/json",
    }
    resp = await client.post("/v0/integrations/slack/events", content=raw, headers=headers)
    assert resp.status_code == 403

    # Unknown team → 403 (never resolves a workspace).
    unknown = json.dumps({"type": "event_callback", "team_id": "T_UNKNOWN", "event": {}}).encode()
    r2 = await client.post(
        "/v0/integrations/slack/events",
        content=unknown,
        headers={
            slack_sign.SIGNATURE_HEADER: slack_sign.compute_signature(SIGNING_SECRET, ts, unknown),
            slack_sign.TIMESTAMP_HEADER: str(ts),
        },
    )
    assert r2.status_code == 403


async def test_url_verification_handshake(client: httpx.AsyncClient) -> None:
    raw = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()
    resp = await client.post("/v0/integrations/slack/events", content=raw)
    assert resp.status_code == 200
    assert resp.json()["challenge"] == "abc123"
