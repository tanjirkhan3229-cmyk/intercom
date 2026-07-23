"""Realtime-fanout consumer acceptance (P0.4, RFC-001 §6.3).

Proves the outbox → Centrifugo path:
- conversation events fan out to the right channels (conv + inbox buckets);
- despite at-least-once stream redelivery (relay crash mid-batch), each event is published exactly
  once (dedupe by outbox_id + client would dedupe by part_id);
- **cross-tenant isolation**: a workspace's events never land on another workspace's channels.

The gateway isn't running under test, so we inject a fake publisher that records every publish —
the same shape the real ``relay.core.realtime.publish`` would send.
"""

from __future__ import annotations

from collections import Counter
from typing import Any
from uuid import uuid4

import httpx
import psycopg
import pytest

from relay.core import realtime_fanout
from relay.core.redis import get_redis, get_redis_sync

pytestmark = pytest.mark.integration


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _owner(client: httpx.AsyncClient, name: str) -> tuple[str, str]:
    resp = await client.post(
        "/v0/auth/signup",
        json={
            "workspace_name": name,
            "email": f"owner-{uuid4().hex}@example.com",
            "password": "password123",
            "name": "Owner",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["access_token"], body["workspace"]["id"]


async def _conversation_with_reply(client: httpx.AsyncClient, tok: str) -> dict[str, Any]:
    contact = (
        await client.post(
            "/v0/contacts/identify", json={"external_id": uuid4().hex}, headers=_auth(tok)
        )
    ).json()
    conv = (
        await client.post(
            "/v0/conversations",
            json={"contact_id": contact["id"], "body": "hi"},
            headers=_auth(tok),
        )
    ).json()
    await client.post(
        f"/v0/conversations/{conv['id']}/reply", json={"body": "hello"}, headers=_auth(tok)
    )
    return conv


def _drain_outbox_with_crash() -> None:
    """Populate the ``relay:outbox`` stream, forcing a redelivery: publish a batch, roll back before
    the delete (simulated relay crash), then drain properly. The stream now holds duplicates — the
    fanout must collapse them to an exactly-once effect."""
    from relay.core import outbox_relay as relay
    from relay.settings import get_settings

    dsn = get_settings().database_url_psycopg
    redis = get_redis_sync()
    with psycopg.connect(dsn) as conn:
        conn.autocommit = False
        crash_batch = relay._fetch_pending(conn, 1000)
        relay._publish_to_stream(redis, crash_batch)  # published once...
        conn.rollback()  # ...then the "relay" dies before deleting → rows still pending
        relay.drain(conn, redis)  # restart: republish (dupes) + delete


async def test_fanout_publishes_exactly_once_and_isolates_tenants(
    client: httpx.AsyncClient,
) -> None:
    tok_a, ws_a = await _owner(client, "Alpha")
    tok_b, ws_b = await _owner(client, "Bravo")
    conv_a = await _conversation_with_reply(client, tok_a)
    conv_b = await _conversation_with_reply(client, tok_b)

    _drain_outbox_with_crash()

    # Run the fanout over the stream with a fake publisher.
    published: list[tuple[str, dict[str, Any]]] = []

    async def fake_publish(channel: str, data: dict[str, Any]) -> None:
        published.append((channel, data))

    redis = get_redis()
    await realtime_fanout.ensure_group(redis)
    first = await realtime_fanout.consume_once(redis, fake_publish, count=1000)
    assert first > 0
    # Re-running consumes nothing new (all acked; dedupe markers set).
    second = await realtime_fanout.consume_once(redis, fake_publish, count=1000)
    assert second == 0

    conv_events = [(ch, d) for ch, d in published if d.get("topic", "").startswith("conversation.")]
    assert conv_events, "expected conversation events fanned out"

    # --- Cross-tenant isolation: every publish's CHANNEL matches its PAYLOAD's workspace. ---
    # A ws-A event can only ever reach a ws-A channel, and vice versa — this is the leakage proof.
    for channel, data in conv_events:
        if channel.startswith("conv:"):
            assert channel == f"conv:{data['conversation_id']}"
        elif channel.startswith("inbox:"):
            assert channel.split(":")[1] == data["workspace_id"]
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected channel {channel!r}")

    a_channels = {ch for ch, d in conv_events if d["workspace_id"] == ws_a}
    b_channels = {ch for ch, d in conv_events if d["workspace_id"] == ws_b}
    assert a_channels and b_channels
    assert a_channels.isdisjoint(b_channels)  # no shared channel across workspaces
    assert f"conv:{conv_a['id']}" in a_channels
    assert f"conv:{conv_b['id']}" in b_channels
    assert f"conv:{conv_a['id']}" not in b_channels

    # --- Exactly-once: each part reaches its conv channel once despite stream redelivery. ---
    a_conv_channel = f"conv:{conv_a['id']}"
    part_publishes = Counter(
        d["part_id"]
        for ch, d in conv_events
        if ch == a_conv_channel and d.get("topic") == "conversation.part.created"
    )
    assert part_publishes  # at least the opening contact comment + the agent reply
    assert all(count == 1 for count in part_publishes.values()), part_publishes
