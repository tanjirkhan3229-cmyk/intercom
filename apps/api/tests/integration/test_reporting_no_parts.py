"""P0.9 acceptance 4: **no reporting query touches ``conversation_parts`` raw.**

The prompt suggests proving this via ``pg_stat_statements``. That extension must be loaded through
``shared_preload_libraries`` at server start, which the shared test-container Postgres does not do —
enabling it there would perturb every other test. We prove the same guarantee more directly and
deterministically:

1. **Read paths** — a SQLAlchemy ``before_cursor_execute`` hook captures *every* SQL statement the
   four reporting endpoints execute; we assert none references ``conversation_parts``.
2. **Rollup path** — the ``relay_reporting_rollup`` function runs its aggregation in the database
   (raw psycopg, not the ORM engine), so we assert its stored source (``pg_proc.prosrc``) never
   references ``conversation_parts``.

Together these cover the whole reporting query surface (endpoints + rollup) — a stricter check than
scanning ``pg_stat_statements`` after the fact.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import httpx
import psycopg
import pytest
from sqlalchemy import event, text

from relay.core import outbox_relay
from relay.core.db import get_engine, session_scope
from relay.core.redis import get_redis, get_redis_sync
from relay.modules.reporting import consumer as reporting_consumer
from relay.modules.reporting.tasks import compute_daily_rollups
from relay.settings import get_settings

pytestmark = pytest.mark.integration

PASSWORD = "password123"
TODAY = dt.datetime.now(dt.UTC).date().isoformat()


async def _owner(client: httpx.AsyncClient, ws_name: str) -> str:
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
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_lifecycle(client: httpx.AsyncClient, tok: str) -> None:
    contact = (
        await client.post(
            "/v0/contacts/identify", json={"external_id": uuid4().hex}, headers=_auth(tok)
        )
    ).json()
    conv = (
        await client.post(
            "/v0/conversations",
            json={"contact_id": contact["id"], "body": "hello"},
            headers=_auth(tok),
        )
    ).json()
    cid = conv["id"]
    await client.post(f"/v0/conversations/{cid}/reply", json={"body": "hi"}, headers=_auth(tok))
    await client.post(f"/v0/conversations/{cid}/rating", json={"rating": 4}, headers=_auth(tok))
    await client.post(
        f"/v0/conversations/{cid}/state", json={"state": "closed"}, headers=_auth(tok)
    )


async def _project_and_roll() -> None:
    dsn = get_settings().database_url_psycopg
    redis_sync = get_redis_sync()
    with psycopg.connect(dsn) as conn:
        conn.autocommit = False
        outbox_relay.drain(conn, redis_sync)
    redis = get_redis()
    await reporting_consumer.ensure_group(redis)
    while (await reporting_consumer.consume_once(redis, count=1000)).entries_read > 0:
        pass
    compute_daily_rollups(TODAY)


class _Capture:
    """Records SQL text for every cursor execution on the async engine's underlying sync engine."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def __call__(self, conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        self.statements.append(statement)


def _listen() -> Iterator[_Capture]:
    sync_engine = get_engine().sync_engine
    cap = _Capture()
    event.listen(sync_engine, "before_cursor_execute", cap)
    try:
        yield cap
    finally:
        event.remove(sync_engine, "before_cursor_execute", cap)


async def test_reporting_endpoints_never_query_conversation_parts(
    client: httpx.AsyncClient,
) -> None:
    tok = await _owner(client, "NoParts")
    await _seed_lifecycle(client, tok)
    await _project_and_roll()

    params = {"from": TODAY, "to": TODAY}
    paths = [
        ("/v0/reports/volume", params),
        ("/v0/reports/responsiveness", params),
        ("/v0/reports/csat", params),
        ("/v0/reports/queue", None),
    ]

    listener = _listen()
    cap = next(listener)
    try:
        for path, p in paths:
            r = await client.get(path, params=p, headers=_auth(tok))
            assert r.status_code == 200, r.text
    finally:
        next(listener, None)  # triggers the finally in _listen (event.remove)

    assert cap.statements, "expected the reporting endpoints to execute SQL"
    offending = [s for s in cap.statements if "conversation_parts" in s.lower()]
    assert not offending, f"reporting queried conversation_parts: {offending}"


async def test_rollup_function_source_never_references_conversation_parts(
    client: httpx.AsyncClient,
) -> None:
    tok = await _owner(client, "NoPartsFn")
    await _seed_lifecycle(client, tok)
    await _project_and_roll()

    async with session_scope() as session:  # no GUC needed: pg_proc is catalog, not tenant data
        source = (
            await session.execute(
                text("SELECT prosrc FROM pg_proc WHERE proname = 'relay_reporting_rollup'")
            )
        ).scalar_one()
    assert "conversation_parts" not in source.lower()
    assert "conversation_metrics" in source.lower()  # sanity: it does read the projection
