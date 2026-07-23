"""Webhooks: subscription CRUD, topic validation, tenant isolation, dispatch consumer (P0.11)."""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from collections.abc import Iterator
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from relay.core.db import session_scope
from relay.core.ids import IdPrefix, decode_public_id, uuid7
from relay.core.outbox import OUTBOX_STREAM, OutboxMessage
from relay.core.redis import get_redis
from relay.modules.webhooks import consumer
from relay.modules.webhooks.models import WebhookDelivery, WebhookSubscription

pytestmark = pytest.mark.integration

PASSWORD = "password123"
LOCAL_URL = "http://127.0.0.1:9/hook"


def _uuid7_at(ms: int) -> uuid.UUID:
    """A UUIDv7 whose embedded timestamp is ``ms`` (to forge an 'old' source event id)."""
    value = (ms & 0xFFFFFFFFFFFF) << 80
    value |= 0x7 << 76  # version
    value |= 0x2 << 62  # variant
    value |= 0x1234_5678_9ABC  # arbitrary randomness bits
    return uuid.UUID(int=value)


@pytest.fixture
def allow_private_webhooks() -> Iterator[None]:
    from relay.settings import get_settings

    old = os.environ.get("WEBHOOK_ALLOW_PRIVATE_TARGETS")
    os.environ["WEBHOOK_ALLOW_PRIVATE_TARGETS"] = "true"
    get_settings.cache_clear()
    yield
    if old is None:
        os.environ.pop("WEBHOOK_ALLOW_PRIVATE_TARGETS", None)
    else:
        os.environ["WEBHOOK_ALLOW_PRIVATE_TARGETS"] = old
    get_settings.cache_clear()


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


async def test_subscription_crud(client: httpx.AsyncClient, allow_private_webhooks: None) -> None:
    token, _ = await _owner(client)
    create = await client.post(
        "/v0/webhook_subscriptions",
        json={"url": LOCAL_URL, "topics": ["contact.created", "conversation.created"]},
        headers=_auth(token),
    )
    assert create.status_code == 201, create.text
    body = create.json()
    assert body["secret"]  # returned exactly once
    assert body["secret_last4"] == body["secret"][-4:]
    assert body["status"] == "active"
    whk = body["id"]

    got = await client.get(f"/v0/webhook_subscriptions/{whk}", headers=_auth(token))
    assert got.status_code == 200
    assert "secret" not in got.json()  # never exposed again

    listing = await client.get("/v0/webhook_subscriptions", headers=_auth(token))
    assert listing.status_code == 200
    assert len(listing.json()["items"]) == 1

    patched = await client.patch(
        f"/v0/webhook_subscriptions/{whk}",
        json={"topics": ["contact.updated"]},
        headers=_auth(token),
    )
    assert patched.status_code == 200
    assert patched.json()["topics"] == ["contact.updated"]

    deleted = await client.delete(f"/v0/webhook_subscriptions/{whk}", headers=_auth(token))
    assert deleted.status_code == 204
    assert (
        await client.get(f"/v0/webhook_subscriptions/{whk}", headers=_auth(token))
    ).status_code == 404


async def test_unknown_topic_rejected(
    client: httpx.AsyncClient, allow_private_webhooks: None
) -> None:
    token, _ = await _owner(client)
    r = await client.post(
        "/v0/webhook_subscriptions",
        json={"url": LOCAL_URL, "topics": ["bogus.topic"]},
        headers=_auth(token),
    )
    assert r.status_code == 422


async def test_ssrf_rejected_at_create_in_prod_mode(client: httpx.AsyncClient) -> None:
    # No allow_private fixture → prod egress policy: an http/loopback target is rejected (422).
    token, _ = await _owner(client)
    r = await client.post(
        "/v0/webhook_subscriptions",
        json={"url": LOCAL_URL, "topics": ["contact.created"]},
        headers=_auth(token),
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_webhook_url"


async def test_cross_tenant_isolation(
    client: httpx.AsyncClient, allow_private_webhooks: None
) -> None:
    token_a, _ = await _owner(client, "A")
    created = await client.post(
        "/v0/webhook_subscriptions",
        json={"url": LOCAL_URL, "topics": ["contact.created"]},
        headers=_auth(token_a),
    )
    whk_a = created.json()["id"]
    token_b, _ = await _owner(client, "B")
    assert (
        await client.get(f"/v0/webhook_subscriptions/{whk_a}", headers=_auth(token_b))
    ).status_code == 404
    assert (await client.get("/v0/webhook_subscriptions", headers=_auth(token_b))).json()[
        "items"
    ] == []


async def test_rls_backstop_hides_rows_without_guc(
    client: httpx.AsyncClient, allow_private_webhooks: None
) -> None:
    token, _ = await _owner(client)
    await client.post(
        "/v0/webhook_subscriptions",
        json={"url": LOCAL_URL, "topics": ["contact.created"]},
        headers=_auth(token),
    )
    # With no app.ws GUC set, RLS must return zero rows (the defense-in-depth backstop).
    async with session_scope(None) as session:
        rows = (await session.scalars(select(WebhookSubscription))).all()
    assert rows == []


async def test_contact_writes_emit_outbox_events(
    client: httpx.AsyncClient, allow_private_webhooks: None
) -> None:
    """Verify identify emits crm.contact.created (new) and crm.contact.updated (existing)."""
    token, ws = await _owner(client)
    r1 = await client.post(
        "/v0/contacts/identify",
        json={"external_id": "u1", "email": "a@example.com"},
        headers=_auth(token),
    )
    assert r1.status_code == 200, r1.text
    r2 = await client.post(
        "/v0/contacts/identify",
        json={"external_id": "u1", "name": "Renamed"},
        headers=_auth(token),
    )
    assert r2.status_code == 200, r2.text

    # The outbox is infrastructure (no RLS); scope to this workspace's payload to stay isolated.
    async with session_scope(None) as session:
        rows = (
            await session.scalars(
                select(OutboxMessage).where(OutboxMessage.payload["workspace_id"].astext == ws)
            )
        ).all()
    topics = {m.topic for m in rows}
    assert "crm.contact.created" in topics
    assert "crm.contact.updated" in topics


async def test_dispatch_consumer_creates_delivery(
    client: httpx.AsyncClient, allow_private_webhooks: None
) -> None:
    token, ws = await _owner(client)
    await client.post(
        "/v0/webhook_subscriptions",
        json={"url": LOCAL_URL, "topics": ["contact.created"]},
        headers=_auth(token),
    )
    # Simulate the outbox relay: push a contact.created event onto the stream, run the consumer.
    redis = get_redis()
    await consumer.ensure_group(redis)
    outbox_id = str(uuid7())  # realistic: production outbox ids are uuid7
    await redis.xadd(
        OUTBOX_STREAM,
        {
            "outbox_id": outbox_id,
            "aggregate": "contact",
            "aggregate_id": str(uuid4()),
            "seq": "1",
            "topic": "crm.contact.created",
            "payload": json.dumps({"workspace_id": ws, "contact_id": "usr_x"}),
        },
    )
    handled = await consumer.consume_once(redis, block_ms=100)
    assert handled == 1

    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
    async with session_scope(ws_uuid) as session:
        deliveries = (await session.scalars(select(WebhookDelivery))).all()
    assert len(deliveries) == 1
    d = deliveries[0]
    assert d.topic == "contact.created"  # translated from the internal outbox topic
    assert d.status == "pending"
    assert str(d.outbox_id) == outbox_id

    # RLS backstop on the partitioned child: with no app.ws GUC, zero delivery rows are visible.
    async with session_scope(None) as session:
        assert (await session.scalars(select(WebhookDelivery))).all() == []

    # Idempotent: replaying the same outbox entry does not create a second delivery row.
    await redis.xadd(
        OUTBOX_STREAM,
        {
            "outbox_id": outbox_id,
            "aggregate": "contact",
            "aggregate_id": str(uuid4()),
            "seq": "1",
            "topic": "crm.contact.created",
            "payload": json.dumps({"workspace_id": ws, "contact_id": "usr_x"}),
        },
    )
    await consumer.consume_once(redis, block_ms=100)
    async with session_scope(ws_uuid) as session:
        again = (await session.scalars(select(WebhookDelivery))).all()
    assert len(again) == 1


async def test_redispatch_without_marker_is_at_least_once(
    client: httpx.AsyncClient, allow_private_webhooks: None
) -> None:
    """Delivery is at-least-once: the Redis dispatch marker collapses the common relay redelivery
    (proven in the test above). If that marker is lost, re-processing the same event DOES create a
    second delivery row — both carry the same event id, so receivers dedupe downstream. The DB does
    not (and by design cannot cheaply) dedupe cross-dispatch, since created_at is the dispatch
    instant (required for correct partition routing + retry-window anchoring)."""
    token, ws = await _owner(client)
    await client.post(
        "/v0/webhook_subscriptions",
        json={"url": LOCAL_URL, "topics": ["contact.created"]},
        headers=_auth(token),
    )
    redis = get_redis()
    await consumer.ensure_group(redis)
    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
    outbox_id = str(uuid7())
    fields = {
        "outbox_id": outbox_id,
        "aggregate": "contact",
        "aggregate_id": str(uuid4()),
        "seq": "1",
        "topic": "crm.contact.created",
        "payload": json.dumps({"workspace_id": ws, "contact_id": "usr_x"}),
    }
    await redis.xadd(OUTBOX_STREAM, fields)
    assert await consumer.consume_once(redis, block_ms=100) == 1

    # Drop the marker → the fast-path dedupe is gone, so the re-dispatch is delivered again.
    await redis.delete(f"{consumer._DEDUPE_PREFIX}{outbox_id}")
    await redis.xadd(OUTBOX_STREAM, fields)
    await consumer.consume_once(redis, block_ms=100)

    async with session_scope(ws_uuid) as session:
        rows = (await session.scalars(select(WebhookDelivery))).all()
    assert len(rows) == 2  # at-least-once
    assert {str(r.outbox_id) for r in rows} == {outbox_id}  # same event id → receiver dedupes


async def test_dispatch_stamps_created_at_at_dispatch_time_not_event_time(
    client: httpx.AsyncClient, allow_private_webhooks: None
) -> None:
    """Regression: the delivery's created_at is the DISPATCH instant, not the (possibly old) source
    event's timestamp. Deriving it from the event caused two HIGH bugs — a backlog-dispatched old
    event missed the seeded partition (poison loop) and exhausted its retry window on attempt 1."""
    token, ws = await _owner(client)
    await client.post(
        "/v0/webhook_subscriptions",
        json={"url": LOCAL_URL, "topics": ["contact.created"]},
        headers=_auth(token),
    )
    redis = get_redis()
    await consumer.ensure_group(redis)
    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
    # Source event minted ~200 days ago (its month partition would not exist / be purged).
    old_ms = int((dt.datetime.now(dt.UTC) - dt.timedelta(days=200)).timestamp() * 1000)
    await redis.xadd(
        OUTBOX_STREAM,
        {
            "outbox_id": str(_uuid7_at(old_ms)),
            "aggregate": "contact",
            "aggregate_id": str(uuid4()),
            "seq": "1",
            "topic": "crm.contact.created",
            "payload": json.dumps({"workspace_id": ws, "contact_id": "usr_x"}),
        },
    )
    assert await consumer.consume_once(redis, block_ms=100) == 1  # inserted, no partition error

    async with session_scope(ws_uuid) as session:
        row = (await session.scalars(select(WebhookDelivery))).one()
    # created_at is ~now (dispatch), NOT ~200 days ago — so it lands in the current partition and
    # its retry window starts at dispatch.
    assert dt.datetime.now(dt.UTC) - row.created_at < dt.timedelta(minutes=5)


async def test_create_subscription_never_stores_secret_in_idempotency_ledger(
    client: httpx.AsyncClient, allow_private_webhooks: None
) -> None:
    """The 201 create response carries the one-time signing secret, so create must NOT be
    @idempotent — otherwise @idempotent would persist the plaintext secret as cleartext in
    idempotency_keys, defeating the Fernet-at-rest design (any DB read could forge signatures)."""
    from relay.core.idempotency import IdempotencyKey

    token, ws = await _owner(client)
    r = await client.post(
        "/v0/webhook_subscriptions",
        json={"url": LOCAL_URL, "topics": ["contact.created"]},
        headers={**_auth(token), "Idempotency-Key": "k-secret-leak-check"},
    )
    assert r.status_code == 201, r.text
    secret = r.json()["secret"]
    assert secret  # returned once on create

    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
    async with session_scope(ws_uuid) as session:
        rows = (await session.scalars(select(IdempotencyKey))).all()
    # The plaintext secret must not appear in any stored idempotency response.
    assert all(secret not in json.dumps(row.response or {}) for row in rows)
