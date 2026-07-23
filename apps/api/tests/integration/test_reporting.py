"""Reporting consumer + projection integration tests (P0.9, RFC-000 §2.9, RFC-002 §5.6).

Drives the messaging API to emit real outbox events, drains them to the ``relay:outbox`` stream
(exactly as the outbox relay does), runs the ``reporting-metrics`` consumer over the stream, and
asserts the ``conversation_metrics`` projection. Covers:
- metrics are projected correctly from a full conversation lifecycle;
- **idempotent replay**: re-consuming every event (a second group from id 0) leaves the row
  unchanged (``last_seq`` guard) — at-least-once delivery yields an exactly-once effect;
- **cross-tenant isolation**: a workspace's metrics are invisible to another workspace, and an
  unset ``app.ws`` GUC returns zero rows (RLS forced).
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import psycopg
import pytest
from sqlalchemy import func, select

from relay.core import outbox_relay
from relay.core.db import session_scope
from relay.core.ids import IdPrefix, decode_public_id
from relay.core.redis import get_redis, get_redis_sync
from relay.modules.reporting import consumer as reporting_consumer
from relay.modules.reporting.models import ConversationMetric
from relay.settings import get_settings

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


async def _full_lifecycle(client: httpx.AsyncClient, tok: str) -> str:
    """open → agent reply → rating → close. Returns the conversation public id."""
    contact = (
        await client.post(
            "/v0/contacts/identify", json={"external_id": uuid4().hex}, headers=_auth(tok)
        )
    ).json()
    conv = (
        await client.post(
            "/v0/conversations",
            json={"contact_id": contact["id"], "body": "hello, I need help"},
            headers=_auth(tok),
        )
    ).json()
    cid = conv["id"]
    r = await client.post(
        f"/v0/conversations/{cid}/reply", json={"body": "happy to help"}, headers=_auth(tok)
    )
    assert r.status_code == 201, r.text
    r = await client.post(f"/v0/conversations/{cid}/rating", json={"rating": 5}, headers=_auth(tok))
    assert r.status_code == 201, r.text
    r = await client.post(
        f"/v0/conversations/{cid}/state", json={"state": "closed"}, headers=_auth(tok)
    )
    assert r.status_code == 200, r.text
    return cid


def _drain_outbox() -> None:
    """Publish all pending outbox rows to the Redis stream (what ``relay outbox-relay`` does)."""
    dsn = get_settings().database_url_psycopg
    redis = get_redis_sync()
    with psycopg.connect(dsn) as conn:
        conn.autocommit = False
        outbox_relay.drain(conn, redis)


async def _metric_row(ws_pub: str, conv_pub: str) -> ConversationMetric:
    ws = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    cid = decode_public_id(IdPrefix.CONVERSATION, conv_pub)
    async with session_scope(ws) as session:
        return (
            await session.execute(
                select(ConversationMetric).where(ConversationMetric.conversation_id == cid)
            )
        ).scalar_one()


async def test_metrics_projected_from_lifecycle(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client, "Reports")
    conv = await _full_lifecycle(client, tok)

    _drain_outbox()
    redis = get_redis()
    await reporting_consumer.ensure_group(redis)
    result = await reporting_consumer.consume_once(redis, count=1000)
    assert result.applied > 0

    m = await _metric_row(ws, conv)
    assert m.opened_at is not None
    assert m.first_admin_reply_at is not None
    assert m.first_response_s is not None and m.first_response_s >= 0
    assert m.replies_count == 1  # one agent reply (the opening part is the contact's)
    assert m.rating == 5
    assert m.rated_at is not None
    assert m.closed_at is not None
    assert m.resolution_s is not None and m.resolution_s >= 0
    assert m.reopen_count == 0
    assert m.last_seq > 0


async def test_replay_is_idempotent(client: httpx.AsyncClient) -> None:
    """Re-consuming every stream entry via a fresh group (from id 0) must not double-count."""
    tok, ws = await _owner(client, "ReportsIdem")
    conv = await _full_lifecycle(client, tok)

    _drain_outbox()
    redis = get_redis()
    await reporting_consumer.ensure_group(redis)
    await reporting_consumer.consume_once(redis, count=1000)
    first = await _metric_row(ws, conv)
    first_replies, first_seq, first_rating = first.replies_count, first.last_seq, first.rating

    # A brand-new group re-reads the entire stream from the beginning → replays every event.
    await reporting_consumer.ensure_group(redis, group="reporting-metrics-replay")
    replay = await reporting_consumer.consume_once(
        redis, group="reporting-metrics-replay", count=1000
    )
    assert replay.entries_read > 0  # the fresh group re-read the whole stream
    assert replay.applied == 0  # but every event's seq <= last_seq → no-ops

    second = await _metric_row(ws, conv)
    assert (second.replies_count, second.last_seq, second.rating) == (
        first_replies,
        first_seq,
        first_rating,
    )


async def test_cross_tenant_isolation_and_unset_guc(client: httpx.AsyncClient) -> None:
    tok_a, ws_a = await _owner(client, "TenantA")
    tok_b, ws_b = await _owner(client, "TenantB")
    conv_a = await _full_lifecycle(client, tok_a)
    await _full_lifecycle(client, tok_b)

    _drain_outbox()
    redis = get_redis()
    await reporting_consumer.ensure_group(redis)
    await reporting_consumer.consume_once(redis, count=1000)

    # B cannot see A's conversation metric; each workspace sees only its own single row.
    a_uuid = decode_public_id(IdPrefix.WORKSPACE, ws_a)
    b_uuid = decode_public_id(IdPrefix.WORKSPACE, ws_b)
    conv_a_uuid = decode_public_id(IdPrefix.CONVERSATION, conv_a)

    async with session_scope(b_uuid) as s:
        leaked = (
            await s.execute(
                select(func.count())
                .select_from(ConversationMetric)
                .where(ConversationMetric.conversation_id == conv_a_uuid)
            )
        ).scalar_one()
        assert leaked == 0
        own = (await s.execute(select(func.count()).select_from(ConversationMetric))).scalar_one()
        assert own == 1

    async with session_scope(a_uuid) as s:
        own_a = (await s.execute(select(func.count()).select_from(ConversationMetric))).scalar_one()
        assert own_a == 1

    # Unset app.ws GUC → RLS returns zero rows (forced policy, defense-in-depth).
    async with session_scope() as s:
        none_visible = (
            await s.execute(select(func.count()).select_from(ConversationMetric))
        ).scalar_one()
        assert none_visible == 0
