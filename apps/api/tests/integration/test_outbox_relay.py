"""Outbox relay chaos test (P0.3 acceptance, RFC-001 §6.5).

Acceptance: kill the relay mid-batch (after publishing to Redis, before marking rows done) and
restart — every outbox row is delivered *at least once*, a consumer dedupes by ``outbox_id`` to
an exactly-once effect, per-aggregate ordering is preserved, and published rows are cleaned up.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from uuid import uuid4

import httpx
import psycopg
import pytest

from relay.core import outbox_relay
from relay.core.outbox import OUTBOX_STREAM
from relay.core.redis import get_redis_sync
from relay.settings import get_settings

pytestmark = pytest.mark.integration

PASSWORD = "password123"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _owner(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        "/v0/auth/signup",
        json={
            "workspace_name": "Chaos",
            "email": f"owner-{uuid4().hex}@example.com",
            "password": PASSWORD,
            "name": "Owner",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["access_token"]


async def _conversation_with_reply(client: httpx.AsyncClient, tok: str) -> None:
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


async def test_relay_at_least_once_with_consumer_dedupe(client: httpx.AsyncClient) -> None:
    tok = await _owner(client)
    # Generate outbox rows across two aggregates (each: created + part.created + reply part).
    for _ in range(2):
        await _conversation_with_reply(client, tok)

    dsn = get_settings().database_url_psycopg
    redis = get_redis_sync()

    with psycopg.connect(dsn) as conn:
        conn.autocommit = False
        emitted = {str(r["id"]) for r in outbox_relay._fetch_pending(conn, 1000)}
        assert len(emitted) >= 6  # 2 aggregates x (created + 2 part.created)

        # --- crash mid-batch: publish to Redis, then die before the delete/commit ---
        crash_batch = outbox_relay._fetch_pending(conn, 1000)
        outbox_relay._publish_to_stream(redis, crash_batch)
        conn.rollback()  # the relay process died; its (delete-less) txn rolls back

        # --- restart: drain re-reads ALL still-pending rows, republishes, deletes ---
        published = outbox_relay.drain(conn, redis)
        assert published == len(emitted)  # nothing was lost/pre-deleted

    entries = redis.xrange(OUTBOX_STREAM)
    counts = Counter(fields["outbox_id"] for _id, fields in entries)

    # At-least-once: every emitted row was delivered at least once (no loss).
    assert set(counts) == emitted
    # The crash caused redeliveries (at-least-once, not exactly-once on the wire).
    assert any(c >= 2 for c in counts.values())
    # Consumer dedupe by outbox_id collapses to an exactly-once effect.
    assert set(counts.keys()) == emitted

    # Per-aggregate ordering preserved: first delivery of each row is seq-ascending per aggregate.
    seen: set[str] = set()
    by_agg: dict[str, list[int]] = defaultdict(list)
    for _id, fields in entries:
        if fields["outbox_id"] in seen:
            continue
        seen.add(fields["outbox_id"])
        by_agg[fields["aggregate_id"]].append(int(fields["seq"]))
    for seqs in by_agg.values():
        assert seqs == sorted(seqs), seqs

    # Aggressive cleanup: the outbox is empty after a successful drain.
    with psycopg.connect(dsn) as check:
        remaining = check.execute("SELECT count(*) FROM outbox").fetchone()
    assert remaining is not None and remaining[0] == 0
