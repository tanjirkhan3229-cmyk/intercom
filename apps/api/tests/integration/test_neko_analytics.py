"""Neko analytics v0 — P1.4 acceptance (RFC-003 §8 analytics, RFC-002 §5.6 reporting spine).

Drives real Neko turns (hermetic provider, RLS forced), projects the reporting spine
(outbox → reporting-metrics consumer → conversation_metrics), rolls up
(``relay_neko_rollup`` → ``neko_daily_rollups``), and asserts the analytics endpoints. Covers:

- **reconciliation** (P1.4 acceptance 1): the analytics ``resolutions`` equals the billing meter's
  net ``SUM(qty)`` — the exact P1.3 fixture number, sourced from ``usage_records``;
- resolution & deflection rates and the per-day series;
- **handoff-reasons breakdown** via the deterministic spend-cap handoff;
- **CSAT delta** (Neko-touched vs not) from the ``ai_involved`` flag;
- the **run inspector**: workspace-wide searchable/keyset list + a single-run trace (retrieval set,
  decisions, outputs) — including cross-tenant 404;
- **cross-tenant isolation** of ``neko_daily_rollups`` (master rule 1).
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from uuid import uuid4

import httpx
import psycopg
import pytest
from sqlalchemy import func, select

from relay.core import outbox_relay
from relay.core.db import session_scope
from relay.core.ids import IdPrefix, decode_public_id, encode_public_id
from relay.core.redis import get_redis, get_redis_sync
from relay.modules.ai import service as ai_service
from relay.modules.ai.pipeline import run_turn
from relay.modules.billing import service as billing_service
from relay.modules.billing.models import UsageRecord
from relay.modules.knowledge.chunking import Chunk
from relay.modules.knowledge.embeddings import DeterministicEmbedder
from relay.modules.knowledge.indexing import index_chunks
from relay.modules.reporting import consumer as reporting_consumer
from relay.modules.reporting.models import NekoDailyRollup
from relay.modules.reporting.tasks import compute_neko_rollups
from relay.settings import get_settings

pytestmark = pytest.mark.integration

PASSWORD = "correct-horse-battery-staple"
TODAY = dt.datetime.now(dt.UTC).date().isoformat()


# --- helpers (shared shape with test_neko_metering + test_reporting_endpoints) ------------------


async def _owner(client: httpx.AsyncClient, ws_name: str) -> tuple[str, uuid.UUID]:
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
    return body["access_token"], decode_public_id(IdPrefix.WORKSPACE, body["workspace"]["id"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _enable_neko(client: httpx.AsyncClient, token: str, **overrides: object) -> None:
    payload: dict[str, object] = {"enabled": True, "channels": ["chat"]}
    payload.update(overrides)
    r = await client.patch("/v0/ai/settings", json=payload, headers=_auth(token))
    assert r.status_code == 200, r.text


async def _ingest(ws: uuid.UUID, content: str, *, title: str | None = None) -> None:
    async with session_scope(ws) as session:
        await index_chunks(
            session,
            workspace_id=ws,
            source_kind="article",
            source_id=uuid.uuid4(),
            locale="en",
            audience={},
            title=title,
            chunks=[Chunk(chunk_index=0, content=content, heading_path=None, token_count=10)],
            embedder=DeterministicEmbedder(),
            emb_version=1,
        )


async def _new_conversation(
    client: httpx.AsyncClient, token: str, body: str
) -> tuple[uuid.UUID, uuid.UUID]:
    ci = await client.post(
        "/v0/contacts/identify", json={"external_id": uuid4().hex}, headers=_auth(token)
    )
    assert ci.status_code == 200, ci.text
    conv = await client.post(
        "/v0/conversations",
        json={"contact_id": ci.json()["id"], "body": body},
        headers=_auth(token),
    )
    assert conv.status_code == 201, conv.text
    conv_pub = conv.json()["id"]
    parts = (await client.get(f"/v0/conversations/{conv_pub}/parts", headers=_auth(token))).json()[
        "items"
    ]
    comment = next(
        p for p in parts if p["author_kind"] == "contact" and p["part_type"] == "comment"
    )
    return (
        decode_public_id(IdPrefix.CONVERSATION, conv_pub),
        decode_public_id(IdPrefix.PART, comment["id"]),
    )


async def _neko_answers(client: httpx.AsyncClient, token: str, ws: uuid.UUID, q: str) -> uuid.UUID:
    await _ingest(ws, "Refunds are processed within 30 days for any subscription.", title="Refunds")
    conv, part = await _new_conversation(client, token, q)
    result = await run_turn(workspace_id=ws, conversation_id=conv, trigger_part_id=part)
    assert result.outcome == "answered", result
    return conv


async def _rate_and_close(
    client: httpx.AsyncClient, token: str, conv: uuid.UUID, rating: int
) -> None:
    pub = encode_public_id(IdPrefix.CONVERSATION, conv)
    r = await client.post(
        f"/v0/conversations/{pub}/rating", json={"rating": rating}, headers=_auth(token)
    )
    assert r.status_code == 201, r.text
    s = await client.post(
        f"/v0/conversations/{pub}/state", json={"state": "closed"}, headers=_auth(token)
    )
    assert s.status_code == 200, s.text


async def _net_resolutions(ws: uuid.UUID) -> Decimal:
    async with session_scope(ws) as session:
        total = await session.scalar(
            select(func.coalesce(func.sum(UsageRecord.qty), 0)).where(
                UsageRecord.meter == billing_service.RESOLUTION_METER
            )
        )
    return Decimal(total or 0)


async def _rollup_row(ws: uuid.UUID) -> NekoDailyRollup:
    today = dt.date.fromisoformat(TODAY)
    async with session_scope(ws) as session:
        row = (
            await session.execute(select(NekoDailyRollup).where(NekoDailyRollup.day == today))
        ).scalar_one()
    return row


def _drain_outbox() -> None:
    dsn = get_settings().database_url_psycopg
    redis = get_redis_sync()
    with psycopg.connect(dsn) as conn:
        conn.autocommit = False
        outbox_relay.drain(conn, redis)


async def _project() -> None:
    """Drain outbox → run the metrics consumer over the whole stream (projects ai_involved)."""
    _drain_outbox()
    redis = get_redis()
    await reporting_consumer.ensure_group(redis)
    while (await reporting_consumer.consume_once(redis, count=1000)).entries_read > 0:
        pass


# --- reconciliation + resolution/deflection ----------------------------------------------------


async def test_resolution_reconciles_with_metering_and_rates(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "neko-analytics-reconcile")
    await _enable_neko(client, token)
    conv = await _neko_answers(client, token, ws, "How do I get a refund?")
    async with session_scope(ws) as session:  # confirm → meters +1, same txn as the close
        assert await ai_service.confirm_resolution(session, workspace_id=ws, conversation_id=conv)

    await _project()
    compute_neko_rollups(TODAY)

    # P1.4 acceptance 1: analytics resolutions == the billing meter's net (the P1.3 fixture number).
    net = await _net_resolutions(ws)
    assert net == Decimal(1)
    row = await _rollup_row(ws)
    assert row.resolutions == net
    assert row.runs_answered == 1
    assert row.conversations_engaged == 1
    assert row.conversations_answered == 1
    assert row.conversations_handoff == 0

    report = (
        await client.get(
            "/v0/reports/neko", params={"from": TODAY, "to": TODAY}, headers=_auth(token)
        )
    ).json()
    totals = report["totals"]
    assert totals["resolutions"] == 1.0
    assert totals["conversations_engaged"] == 1
    assert totals["resolution_rate"] == 1.0
    assert totals["deflection_rate"] == 1.0  # no handoff → fully deflected
    assert totals["handoff_reasons"] == {}
    point = next(p for p in report["points"] if p["day"] == TODAY)
    assert point["runs_answered"] == 1
    assert point["resolutions"] == 1.0
    assert point["cost_usd"] >= 0


async def test_rollup_rerun_is_idempotent(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "neko-analytics-idem")
    await _enable_neko(client, token)
    await _neko_answers(client, token, ws, "How do I get a refund?")
    await _project()

    def cols(r: NekoDailyRollup) -> dict:
        return {c.name: getattr(r, c.name) for c in NekoDailyRollup.__table__.columns}

    compute_neko_rollups(TODAY)
    first = cols(await _rollup_row(ws))
    compute_neko_rollups(TODAY)
    second = cols(await _rollup_row(ws))
    assert first == second  # byte-identical (ON CONFLICT preserved id + created_at)


# --- handoff-reasons breakdown -----------------------------------------------------------------


async def test_handoff_reasons_breakdown(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "neko-analytics-handoff")
    # Cap at $1.00; two seeded resolutions ($1.98) is over → the next turn hands off (RFC-003 §9).
    await _enable_neko(client, token, monthly_spend_cap_usd=1.0)
    await _ingest(ws, "Refunds are processed within 30 days.", title="Refunds")
    async with session_scope(ws) as session:
        for i in range(2):
            await billing_service.record_usage(
                session,
                workspace_id=ws,
                meter=billing_service.RESOLUTION_METER,
                qty=1,
                source_id=f"seed-{i}",
            )
    conv, part = await _new_conversation(client, token, "How do I get a refund?")
    result = await run_turn(workspace_id=ws, conversation_id=conv, trigger_part_id=part)
    assert result.outcome == "handoff" and result.handoff_reason == "spend_cap_reached"

    await _project()
    compute_neko_rollups(TODAY)

    row = await _rollup_row(ws)
    assert row.runs_handoff == 1
    assert row.conversations_handoff == 1
    assert row.handoff_reasons == {"spend_cap_reached": 1}
    # Reconciliation still holds: analytics resolutions == the two seeded meter rows.
    assert row.resolutions == await _net_resolutions(ws) == Decimal(2)

    report = (
        await client.get(
            "/v0/reports/neko", params={"from": TODAY, "to": TODAY}, headers=_auth(token)
        )
    ).json()
    assert report["totals"]["handoff_reasons"] == {"spend_cap_reached": 1}
    assert report["totals"]["deflection_rate"] == 0.0  # (engaged 1 - handoff 1) / 1


# --- CSAT delta (Neko-touched vs not) ----------------------------------------------------------


async def test_csat_delta_neko_touched_vs_not(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "neko-analytics-csat")
    await _enable_neko(client, token)

    neko_conv = await _neko_answers(client, token, ws, "How do I get a refund?")
    await _rate_and_close(client, token, neko_conv, rating=5)  # Neko-touched, rated 5

    # A purely human conversation (no Neko part), rated 3.
    ci = await client.post(
        "/v0/contacts/identify", json={"external_id": uuid4().hex}, headers=_auth(token)
    )
    conv2 = (
        await client.post(
            "/v0/conversations",
            json={"contact_id": ci.json()["id"], "body": "human please"},
            headers=_auth(token),
        )
    ).json()
    c2 = decode_public_id(IdPrefix.CONVERSATION, conv2["id"])
    r = await client.post(
        f"/v0/conversations/{conv2['id']}/reply",
        json={"body": "a human here"},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    await _rate_and_close(client, token, c2, rating=3)

    await _project()
    csat = (
        await client.get(
            "/v0/reports/neko/csat", params={"from": TODAY, "to": TODAY}, headers=_auth(token)
        )
    ).json()
    assert csat["neko_touched"]["count"] == 1
    assert csat["neko_touched"]["average"] == 5.0
    assert csat["neko_touched"]["distribution"]["5"] == 1
    assert csat["non_neko"]["count"] == 1
    assert csat["non_neko"]["average"] == 3.0
    assert csat["delta"] == 2.0  # 5 - 3


# --- run inspector -----------------------------------------------------------------------------


async def test_run_inspector_search_and_trace(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "neko-analytics-inspector")
    await _enable_neko(client, token)
    conv = await _neko_answers(client, token, ws, "How do I get a refund?")
    conv_pub = encode_public_id(IdPrefix.CONVERSATION, conv)

    # Searchable list, workspace-wide, newest-first.
    listed = (await client.get("/v0/ai/runs", headers=_auth(token))).json()
    assert len(listed["items"]) == 1
    summary = listed["items"][0]
    assert summary["outcome"] == "answered"
    assert summary["query"] == "How do I get a refund?"
    assert summary["conversation_id"] == conv_pub
    run_id = summary["id"]

    # Filters: outcome + question substring + conversation scope.
    assert (
        len(
            (
                await client.get(
                    "/v0/ai/runs", params={"outcome": "answered"}, headers=_auth(token)
                )
            ).json()["items"]
        )
        == 1
    )
    assert (
        len(
            (
                await client.get("/v0/ai/runs", params={"outcome": "handoff"}, headers=_auth(token))
            ).json()["items"]
        )
        == 0
    )
    assert (
        len(
            (await client.get("/v0/ai/runs", params={"q": "refund"}, headers=_auth(token))).json()[
                "items"
            ]
        )
        == 1
    )
    assert (
        len(
            (
                await client.get("/v0/ai/runs", params={"q": "nonsense-zzz"}, headers=_auth(token))
            ).json()["items"]
        )
        == 0
    )

    # Detail carries the full trace — the "why did Neko say X" payload (retrieved evidence content).
    detail = (await client.get(f"/v0/ai/runs/{run_id}", headers=_auth(token))).json()
    assert detail["id"] == run_id
    assert detail["outcome"] == "answered"
    assert detail["retrieved"], "retrieval set present"
    assert "evidence" in detail["trace"]
    assert detail["trace"]["evidence"], "trace evidence (with content) present"

    # Cross-tenant: another workspace cannot read this run (RLS → clean 404).
    other_token, _ = await _owner(client, "neko-analytics-inspector-other")
    r404 = await client.get(f"/v0/ai/runs/{run_id}", headers=_auth(other_token))
    assert r404.status_code == 404


async def test_run_inspector_keyset_pagination(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "neko-analytics-keyset")
    await _enable_neko(client, token)
    await _ingest(ws, "Refunds are processed within 30 days.", title="Refunds")
    for _ in range(3):
        conv, part = await _new_conversation(client, token, "How do I get a refund?")
        await run_turn(workspace_id=ws, conversation_id=conv, trigger_part_id=part)

    first = (await client.get("/v0/ai/runs", params={"limit": 2}, headers=_auth(token))).json()
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = (
        await client.get(
            "/v0/ai/runs", params={"limit": 2, "cursor": first["next_cursor"]}, headers=_auth(token)
        )
    ).json()
    assert len(second["items"]) == 1 and second["next_cursor"] is None
    # Distinct pages, strictly older ids (newest-first keyset).
    seen = {i["id"] for i in first["items"]} | {i["id"] for i in second["items"]}
    assert len(seen) == 3


# --- cross-tenant isolation of the rollup table ------------------------------------------------


async def test_neko_rollups_cross_tenant_isolation(client: httpx.AsyncClient) -> None:
    token_a, ws_a = await _owner(client, "neko-analytics-tenant-a")
    token_b, ws_b = await _owner(client, "neko-analytics-tenant-b")
    await _enable_neko(client, token_a)
    await _neko_answers(client, token_a, ws_a, "How do I get a refund?")
    await _project()
    compute_neko_rollups(TODAY)

    # B sees none of A's rollups; an unset app.ws GUC returns zero rows (RLS forced).
    async with session_scope(ws_b) as s:
        leaked = (
            await s.execute(
                select(func.count())
                .select_from(NekoDailyRollup)
                .where(NekoDailyRollup.workspace_id == ws_a)
            )
        ).scalar_one()
        assert leaked == 0
    async with session_scope() as s:
        assert (
            await s.execute(select(func.count()).select_from(NekoDailyRollup))
        ).scalar_one() == 0

    # And B's analytics endpoint shows no activity.
    report = (
        await client.get(
            "/v0/reports/neko", params={"from": TODAY, "to": TODAY}, headers=_auth(token_b)
        )
    ).json()
    assert report["totals"]["conversations_engaged"] == 0
    assert report["totals"]["resolution_rate"] is None
