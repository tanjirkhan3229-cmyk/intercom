"""Reporting read-endpoint + rollup integration tests (P0.9).

Runs the whole pipeline — messaging API → outbox → ``reporting-metrics`` consumer →
``conversation_metrics`` → ``compute_daily_rollups`` → ``daily_rollups`` — then asserts the four
read endpoints (volume / responsiveness / CSAT / queue). Also proves the rollup is idempotent
(re-run yields byte-identical rows — P0.9 acceptance 2) and the queue monitor is served from a
short-TTL cache.
"""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import httpx
import psycopg
import pytest
from sqlalchemy import func, select, update

from relay.core import outbox_relay
from relay.core.db import session_scope
from relay.core.ids import IdPrefix, decode_public_id, encode_public_id
from relay.core.redis import get_redis, get_redis_sync
from relay.modules.reporting import consumer as reporting_consumer
from relay.modules.reporting import service as reporting_service
from relay.modules.reporting.models import ConversationMetric, DailyRollup
from relay.modules.reporting.tasks import compute_daily_rollups
from relay.settings import get_settings

pytestmark = pytest.mark.integration

PASSWORD = "password123"
TODAY = dt.datetime.now(dt.UTC).date().isoformat()


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


async def _open_conversation(client: httpx.AsyncClient, tok: str) -> str:
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
    return conv["id"]


async def _full_lifecycle(client: httpx.AsyncClient, tok: str, *, rating: int = 5) -> str:
    cid = await _open_conversation(client, tok)
    assert (
        await client.post(
            f"/v0/conversations/{cid}/reply", json={"body": "on it"}, headers=_auth(tok)
        )
    ).status_code == 201
    assert (
        await client.post(
            f"/v0/conversations/{cid}/rating", json={"rating": rating}, headers=_auth(tok)
        )
    ).status_code == 201
    assert (
        await client.post(
            f"/v0/conversations/{cid}/state", json={"state": "closed"}, headers=_auth(tok)
        )
    ).status_code == 200
    return cid


async def _set_metric(ws_pub: str, conv_pub: str, **fields: object) -> None:
    """Directly patch a conversation's metrics row to exercise the rollup's authoritative behaviour
    (a first-team latch NULL->team, or a backdated open day) without team-management endpoints."""
    ws = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    cid = decode_public_id(IdPrefix.CONVERSATION, conv_pub)
    async with session_scope(ws) as s:
        await s.execute(
            update(ConversationMetric)
            .where(ConversationMetric.conversation_id == cid)
            .values(**fields)
        )


def _drain_outbox() -> None:
    dsn = get_settings().database_url_psycopg
    redis = get_redis_sync()
    with psycopg.connect(dsn) as conn:
        conn.autocommit = False
        outbox_relay.drain(conn, redis)


async def _project() -> None:
    """Drain outbox → run the metrics consumer over the whole stream."""
    _drain_outbox()
    redis = get_redis()
    await reporting_consumer.ensure_group(redis)
    while (await reporting_consumer.consume_once(redis, count=1000)).entries_read > 0:
        pass


async def test_volume_responsiveness_csat(client: httpx.AsyncClient) -> None:
    tok, _ = await _owner(client, "Reports")
    await _full_lifecycle(client, tok)
    await _project()
    compute_daily_rollups(TODAY)

    params = {"from": TODAY, "to": TODAY}

    volume = (await client.get("/v0/reports/volume", params=params, headers=_auth(tok))).json()
    today_point = next(p for p in volume["points"] if p["day"] == TODAY)
    assert today_point["opened"] == 1
    assert today_point["closed"] == 1
    assert today_point["replies"] == 1

    resp = (
        await client.get("/v0/reports/responsiveness", params=params, headers=_auth(tok))
    ).json()
    assert resp["first_response"]["count"] == 1
    assert resp["first_response"]["median_s"] is not None
    assert resp["first_response"]["median_s"] >= 0
    assert resp["first_response"]["p90_s"] >= 0

    csat = (await client.get("/v0/reports/csat", params=params, headers=_auth(tok))).json()
    assert csat["count"] == 1
    assert csat["average"] == 5.0
    assert csat["distribution"]["5"] == 1
    assert csat["distribution"]["1"] == 0


async def test_rollup_rerun_is_idempotent(client: httpx.AsyncClient) -> None:
    """P0.9 acceptance: a second rollup run over unchanged metrics produces identical rows."""
    tok, ws = await _owner(client, "RollupIdem")
    await _full_lifecycle(client, tok)
    await _project()

    async def _rows() -> list[dict[str, object]]:
        ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
        async with session_scope(ws_uuid) as s:
            rollups = (
                (await s.execute(select(DailyRollup).order_by(DailyRollup.day))).scalars().all()
            )
            return [
                {c.name: getattr(r, c.name) for c in DailyRollup.__table__.columns} for r in rollups
            ]

    compute_daily_rollups(TODAY)
    first = await _rows()
    assert first, "expected at least one rollup row"

    compute_daily_rollups(TODAY)
    second = await _rows()

    # Byte-identical: same id, created_at, and every metric column (ON CONFLICT preserved the row).
    assert first == second


async def test_queue_monitor_and_cache(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client, "Queue")
    cid = await _open_conversation(client, tok)  # left OPEN and unassigned

    # Mark the owner online, then read the queue (first read computes with presence).
    assert (await client.post("/v0/realtime/presence", headers=_auth(tok))).status_code == 204

    q1 = (await client.get("/v0/reports/queue", headers=_auth(tok))).json()
    assert q1["open"] == 1
    assert q1["unassigned"] == 1
    assert q1["longest_wait_s"] is not None and q1["longest_wait_s"] >= 0
    assert q1["agents_online"] == 1

    # The snapshot is cached with a bounded TTL (the ≤10 s freshness guarantee, R4).
    redis = get_redis()
    cache_key = f"{reporting_service.QUEUE_CACHE_PREFIX}{ws}"
    ttl = await redis.ttl(cache_key)
    assert 0 < ttl <= reporting_service.QUEUE_CACHE_TTL_SECONDS

    # Close the conversation, then read again within the TTL — the cached snapshot still reports it
    # open, proving the monitor is served from cached counts rather than recomputed every call.
    assert (
        await client.post(
            f"/v0/conversations/{cid}/state", json={"state": "closed"}, headers=_auth(tok)
        )
    ).status_code == 200
    q2 = (await client.get("/v0/reports/queue", headers=_auth(tok))).json()
    assert q2 == q1  # served from cache, not recomputed

    # Once the cache entry expires (simulated by eviction), the next read recomputes fresh counts.
    await redis.delete(cache_key)
    q3 = (await client.get("/v0/reports/queue", headers=_auth(tok))).json()
    assert q3["open"] == 0  # conversation is now closed → recomputed


async def test_first_team_latch_does_not_double_count(client: httpx.AsyncClient) -> None:
    """A conversation opened team-less and then routed to a team (team_id latches NULL -> team) must
    not be counted in both the NULL and the team bucket: the rollup is authoritative per day and
    drops the now-empty NULL orphan bucket."""
    tok, ws = await _owner(client, "FirstTeamLatch")
    conv = await _full_lifecycle(client, tok)  # opened + closed today, team = NULL
    await _project()
    compute_daily_rollups(TODAY)  # writes the (ws, today, NULL) bucket

    # The conversation is routed to a team; the consumer latches team_id NULL -> new_team.
    new_team = uuid4()
    await _set_metric(ws, conv, team_id=new_team)
    compute_daily_rollups(TODAY)  # must delete the now-empty NULL bucket, not leave it behind

    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
    today = dt.date.fromisoformat(TODAY)
    async with session_scope(ws_uuid) as s:
        total_rows = (
            await s.execute(
                select(func.count()).select_from(DailyRollup).where(DailyRollup.day == today)
            )
        ).scalar_one()
        assert total_rows == 1  # only the new-team bucket survives; the NULL orphan was deleted
        team_rows = (
            await s.execute(
                select(func.count()).select_from(DailyRollup).where(DailyRollup.team_id == new_team)
            )
        ).scalar_one()
        assert team_rows == 1

    # Workspace-wide (unfiltered) volume must count the conversation exactly once, not twice.
    vol = (
        await client.get(
            "/v0/reports/volume", params={"from": TODAY, "to": TODAY}, headers=_auth(tok)
        )
    ).json()
    assert next(p for p in vol["points"] if p["day"] == TODAY)["opened"] == 1


async def test_multi_conversation_same_day_aggregation(client: httpx.AsyncClient) -> None:
    """CSAT + volume must fan in correctly across multiple conversations on one day."""
    tok, _ = await _owner(client, "MultiAgg")
    await _full_lifecycle(client, tok, rating=5)
    await _full_lifecycle(client, tok, rating=3)
    await _project()
    compute_daily_rollups(TODAY)
    params = {"from": TODAY, "to": TODAY}

    csat = (await client.get("/v0/reports/csat", params=params, headers=_auth(tok))).json()
    assert csat["count"] == 2
    assert csat["average"] == 4.0  # (5 + 3) / 2
    assert csat["distribution"]["5"] == 1
    assert csat["distribution"]["3"] == 1

    vol = (await client.get("/v0/reports/volume", params=params, headers=_auth(tok))).json()
    today_pt = next(p for p in vol["points"] if p["day"] == TODAY)
    assert today_pt["opened"] == 2
    assert today_pt["closed"] == 2
    assert today_pt["replies"] == 2


async def test_team_filtered_reports(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client, "TeamFilter")
    conv = await _full_lifecycle(client, tok)
    await _project()
    team = uuid4()
    await _set_metric(ws, conv, team_id=team)
    compute_daily_rollups(TODAY)

    params = {"from": TODAY, "to": TODAY}
    team_pub = encode_public_id(IdPrefix.TEAM, team)
    other_pub = encode_public_id(IdPrefix.TEAM, uuid4())

    filtered = (
        await client.get(
            "/v0/reports/volume", params={**params, "team_id": team_pub}, headers=_auth(tok)
        )
    ).json()
    assert next(p for p in filtered["points"] if p["day"] == TODAY)["opened"] == 1

    other = (
        await client.get(
            "/v0/reports/volume", params={**params, "team_id": other_pub}, headers=_auth(tok)
        )
    ).json()
    assert all(p["opened"] == 0 for p in other["points"])  # no rows for a different team

    unfiltered = (await client.get("/v0/reports/volume", params=params, headers=_auth(tok))).json()
    assert next(p for p in unfiltered["points"] if p["day"] == TODAY)["opened"] == 1

    # A malformed team id (wrong prefix) is a clean 404, not a 500.
    bad = await client.get(
        "/v0/reports/volume", params={**params, "team_id": "not-a-team"}, headers=_auth(tok)
    )
    assert bad.status_code == 404


async def test_late_rating_is_counted_by_rated_day(client: httpx.AsyncClient) -> None:
    """A rating that lands days after the conversation opened must still appear in CSAT: the rollup
    buckets ratings by rated_at (the recompute window covers the rated day), not by open day."""
    tok, ws = await _owner(client, "LateRating")
    conv = await _full_lifecycle(client, tok)  # opened + rated today
    await _project()

    # The conversation was really opened 3 days ago; the rating still arrived today.
    three_days_ago = dt.datetime.now(dt.UTC) - dt.timedelta(days=3)
    await _set_metric(ws, conv, opened_at=three_days_ago)
    compute_daily_rollups(TODAY)  # rating is bucketed by rated_at=today → inside the window

    start = (dt.date.fromisoformat(TODAY) - dt.timedelta(days=3)).isoformat()
    csat = (
        await client.get(
            "/v0/reports/csat", params={"from": start, "to": TODAY}, headers=_auth(tok)
        )
    ).json()
    assert csat["count"] == 1  # would be 0 if ratings were bucketed by the (un-recomputed) open day
    assert csat["average"] == 5.0
    assert csat["distribution"]["5"] == 1


async def test_daily_rollups_cross_tenant_isolation(client: httpx.AsyncClient) -> None:
    """Master rule 1: the cross-tenant-written daily_rollups table must not leak across workspaces,
    and an unset app.ws GUC must return zero rows (RLS forced)."""
    tok_a, ws_a = await _owner(client, "RollupTenantA")
    tok_b, ws_b = await _owner(client, "RollupTenantB")
    await _full_lifecycle(client, tok_a)
    await _full_lifecycle(client, tok_b)
    await _project()
    compute_daily_rollups(TODAY)

    a_uuid = decode_public_id(IdPrefix.WORKSPACE, ws_a)
    b_uuid = decode_public_id(IdPrefix.WORKSPACE, ws_b)

    async with session_scope(b_uuid) as s:
        leaked = (
            await s.execute(
                select(func.count())
                .select_from(DailyRollup)
                .where(DailyRollup.workspace_id == a_uuid)
            )
        ).scalar_one()
        assert leaked == 0
        own = (await s.execute(select(func.count()).select_from(DailyRollup))).scalar_one()
        assert own >= 1  # B sees its own rollup(s)

    async with session_scope() as s:  # unset GUC → RLS returns zero rows
        none_visible = (await s.execute(select(func.count()).select_from(DailyRollup))).scalar_one()
        assert none_visible == 0
