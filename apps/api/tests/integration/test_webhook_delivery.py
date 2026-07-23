"""Webhook delivery task: signed happy-path + breaker isolation of a hung endpoint (P0.11).

Acceptance (RFC-001 §6.7): "a hanging consumer (test server that sleeps) trips the breaker without
delaying other tenants' deliveries." We drive the sync ``webhooks.deliver`` task directly against
real local HTTP servers.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import threading
import time
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text

from relay.core.db import session_scope
from relay.core.ids import IdPrefix, decode_public_id, encode_public_id, uuid7
from relay.modules.webhooks import signing, tasks
from relay.modules.webhooks.models import WebhookDelivery, WebhookSubscription

pytestmark = pytest.mark.integration

PASSWORD = "password123"


class _Receiver:
    """A throwaway local HTTP endpoint that records requests; optionally sleeps before replying."""

    def __init__(self, *, sleep: float = 0.0, status: int = 200) -> None:
        self.received: list[dict[str, Any]] = []
        self._sleep = sleep
        self._status = status
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # silence
                pass

            def do_POST(self) -> None:  # http.server API name
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                outer.received.append({"headers": dict(self.headers), "body": body})
                if outer._sleep:
                    time.sleep(outer._sleep)
                self.send_response(outer._status)
                self.end_headers()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/hook"

    def __enter__(self) -> _Receiver:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def webhook_env() -> Iterator[None]:
    from relay.settings import get_settings

    overrides = {
        "WEBHOOK_ALLOW_PRIVATE_TARGETS": "true",
        "WEBHOOK_DELIVERY_TIMEOUT_SECONDS": "1",
        "WEBHOOK_BREAKER_THRESHOLD": "2",
        "WEBHOOK_BREAKER_COOLDOWN_SECONDS": "30",
    }
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


async def _subscribe(client: httpx.AsyncClient, token: str, url: str) -> dict[str, str]:
    resp = await client.post(
        "/v0/webhook_subscriptions",
        json={"url": url, "topics": ["contact.created"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _make_delivery(
    ws_uuid: uuid.UUID,
    sub_uuid: uuid.UUID,
    *,
    status: str = "pending",
    next_attempt_at: dt.datetime | None = None,
    attempt: int = 0,
) -> tuple[str, str]:
    """Insert a delivery row (default: a fresh pending one) and return (id, created_at_iso)."""
    did = uuid7()
    now = dt.datetime.now(dt.UTC)
    async with session_scope(ws_uuid) as session:
        session.add(
            WebhookDelivery(
                id=did,
                workspace_id=ws_uuid,
                subscription_id=sub_uuid,
                outbox_id=uuid7(),
                topic="contact.created",
                payload={"contact_id": "usr_x"},
                attempt=attempt,
                status=status,
                next_attempt_at=next_attempt_at,
                created_at=now,
            )
        )
    return str(did), now.isoformat()


async def test_delivery_happy_path_signs_correctly(
    client: httpx.AsyncClient, webhook_env: None
) -> None:
    token, ws = await _owner(client)
    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
    with _Receiver(status=200) as server:
        sub = await _subscribe(client, token, server.url)
        sub_uuid = decode_public_id(IdPrefix.WEBHOOK_SUBSCRIPTION, sub["id"])
        did, created = await _make_delivery(ws_uuid, sub_uuid)

        result = await asyncio.to_thread(tasks.deliver, str(ws_uuid), did, created)
        assert result == "delivered", result

        assert len(server.received) == 1
        req = server.received[0]
        ts = req["headers"]["Relay-Timestamp"]
        sig = req["headers"]["Relay-Signature"]
        assert req["headers"]["Relay-Topic"] == "contact.created"
        assert signing.verify_signature(
            sub["secret"],
            timestamp=int(ts),
            body=req["body"],
            header=sig,
            tolerance_seconds=300,
            now=int(ts),
        )

    async with session_scope(ws_uuid) as session:
        row = (
            await session.scalars(
                select(WebhookDelivery).where(WebhookDelivery.id == uuid.UUID(did))
            )
        ).one()
    assert row.status == "delivered"
    assert row.response_code == 200
    assert row.attempt == 1


async def test_scan_retries_then_deliver_delivers_end_to_end(
    client: httpx.AsyncClient, webhook_env: None
) -> None:
    """Regression for the scan→deliver seam: a due failed row, once the scan enqueues it, MUST
    actually deliver. Previously the scan pre-hid the row (pushed next_attempt_at), so the deliver
    task it enqueued could never claim it and every retry looped forever."""
    token, ws = await _owner(client)
    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
    with _Receiver(status=200) as server:
        sub = await _subscribe(client, token, server.url)
        sub_uuid = decode_public_id(IdPrefix.WEBHOOK_SUBSCRIPTION, sub["id"])
        past = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
        did, created = await _make_delivery(
            ws_uuid, sub_uuid, status="failed", next_attempt_at=past, attempt=1
        )

        assert await asyncio.to_thread(tasks.scan_retries) >= 1  # scan finds the due row

        # The finder must NOT have hidden the row — it stays due so the deliver task can claim it.
        async with session_scope(ws_uuid) as session:
            row = (
                await session.scalars(
                    select(WebhookDelivery).where(WebhookDelivery.id == uuid.UUID(did))
                )
            ).one()
        assert row.status == "failed"
        assert row.next_attempt_at is not None
        assert row.next_attempt_at <= dt.datetime.now(dt.UTC)

        # The deliver task the scan enqueues can claim + deliver it (the seam works end to end).
        assert await asyncio.to_thread(tasks.deliver, str(ws_uuid), did, created) == "delivered"
        assert len(server.received) == 1
        async with session_scope(ws_uuid) as session:
            row = (
                await session.scalars(
                    select(WebhookDelivery).where(WebhookDelivery.id == uuid.UUID(did))
                )
            ).one()
        assert row.status == "delivered"
        assert row.attempt == 2  # incremented from the seeded attempt=1


async def test_redeliver_then_scan_delivers(client: httpx.AsyncClient, webhook_env: None) -> None:
    """The redelivery endpoint records a due row that the scan then drives to delivery."""
    token, ws = await _owner(client)
    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
    auth = {"Authorization": f"Bearer {token}"}
    with _Receiver(status=200) as server:
        sub = await _subscribe(client, token, server.url)
        sub_uuid = decode_public_id(IdPrefix.WEBHOOK_SUBSCRIPTION, sub["id"])
        did, created = await _make_delivery(ws_uuid, sub_uuid)
        assert await asyncio.to_thread(tasks.deliver, str(ws_uuid), did, created) == "delivered"
        assert len(server.received) == 1

        delivery_public = encode_public_id(IdPrefix.WEBHOOK_DELIVERY, uuid.UUID(did))
        r = await client.post(
            f"/v0/webhook_subscriptions/{sub['id']}/deliveries/{delivery_public}/redeliver",
            headers=auth,
        )
        assert r.status_code == 202, r.text
        assert r.json()["status"] == "pending"
        new_uuid = decode_public_id(IdPrefix.WEBHOOK_DELIVERY, r.json()["id"])

        # Redeliver relies on the scan (no inline enqueue): scan finds it, deliver drives it.
        assert await asyncio.to_thread(tasks.scan_retries) >= 1
        async with session_scope(ws_uuid) as session:
            row = (
                await session.scalars(select(WebhookDelivery).where(WebhookDelivery.id == new_uuid))
            ).one()
        assert (
            await asyncio.to_thread(
                tasks.deliver, str(ws_uuid), str(new_uuid), row.created_at.isoformat()
            )
            == "delivered"
        )
        assert len(server.received) == 2  # the event was re-sent


async def test_scan_recovers_stale_delivering_row(
    client: httpx.AsyncClient, webhook_env: None
) -> None:
    """A 'delivering' row whose lease expired (a crashed worker) is re-found and reclaimed."""
    token, ws = await _owner(client)
    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
    with _Receiver(status=200) as server:
        sub = await _subscribe(client, token, server.url)
        sub_uuid = decode_public_id(IdPrefix.WEBHOOK_SUBSCRIPTION, sub["id"])
        expired = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)  # lease long lapsed
        did, created = await _make_delivery(
            ws_uuid, sub_uuid, status="delivering", next_attempt_at=expired, attempt=1
        )
        assert await asyncio.to_thread(tasks.scan_retries) >= 1
        assert await asyncio.to_thread(tasks.deliver, str(ws_uuid), did, created) == "delivered"
        assert len(server.received) == 1


async def test_deliver_with_undecryptable_secret_fails_without_raising(
    client: httpx.AsyncClient, webhook_env: None
) -> None:
    """A rotated/corrupt signing secret is a recorded delivery failure, never a crash-loop."""
    token, ws = await _owner(client)
    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
    with _Receiver(status=200) as server:
        sub = await _subscribe(client, token, server.url)
        sub_uuid = decode_public_id(IdPrefix.WEBHOOK_SUBSCRIPTION, sub["id"])
        # Corrupt the stored ciphertext so decrypt_secret raises InvalidToken at sign time.
        async with session_scope(ws_uuid) as session:
            await session.execute(
                text("UPDATE webhook_subscriptions SET secret_ciphertext = :c WHERE id = :i"),
                {"c": "not-a-valid-fernet-token", "i": str(sub_uuid)},
            )
        did, created = await _make_delivery(ws_uuid, sub_uuid)

        result = await asyncio.to_thread(tasks.deliver, str(ws_uuid), did, created)
        assert result == "failed", result  # not an exception, not a no-op loop
        assert len(server.received) == 0  # never POSTed

        async with session_scope(ws_uuid) as session:
            row = (
                await session.scalars(
                    select(WebhookDelivery).where(WebhookDelivery.id == uuid.UUID(did))
                )
            ).one()
            sub_row = (
                await session.scalars(
                    select(WebhookSubscription).where(WebhookSubscription.id == sub_uuid)
                )
            ).one()
        assert row.status == "failed"
        assert row.error is not None and row.error.startswith("sign:")
        assert sub_row.consecutive_failures == 1  # counted toward auto-disable


async def test_breaker_isolates_hung_endpoint_from_other_tenants(
    client: httpx.AsyncClient, webhook_env: None
) -> None:
    token, ws = await _owner(client)
    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)

    with _Receiver(sleep=3.0, status=200) as slow, _Receiver(status=200) as fast:
        slow_sub = await _subscribe(client, token, slow.url)
        fast_sub = await _subscribe(client, token, fast.url)
        slow_uuid = decode_public_id(IdPrefix.WEBHOOK_SUBSCRIPTION, slow_sub["id"])
        fast_uuid = decode_public_id(IdPrefix.WEBHOOK_SUBSCRIPTION, fast_sub["id"])

        # Two real attempts at the slow endpoint time out (1s < 3s) and fail; breaker opens at 2.
        for _ in range(2):
            did, created = await _make_delivery(ws_uuid, slow_uuid)
            assert await asyncio.to_thread(tasks.deliver, str(ws_uuid), did, created) == "failed"

        # Third attempt fast-fails via the open breaker — no HTTP call, so it returns quickly.
        did, created = await _make_delivery(ws_uuid, slow_uuid)
        t0 = time.monotonic()
        result = await asyncio.to_thread(tasks.deliver, str(ws_uuid), did, created)
        elapsed = time.monotonic() - t0
        assert result == "breaker_open", result
        assert elapsed < 1.0  # did not wait on the timeout / the 3s sleep
        assert len(slow.received) == 2  # the breaker-open attempt never reached the endpoint

        # The healthy tenant's endpoint is entirely unaffected by the hung one.
        did_f, created_f = await _make_delivery(ws_uuid, fast_uuid)
        assert await asyncio.to_thread(tasks.deliver, str(ws_uuid), did_f, created_f) == "delivered"
        assert len(fast.received) == 1
