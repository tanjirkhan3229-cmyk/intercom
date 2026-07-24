"""Neko resolution metering — P1.3 acceptance (RFC-003 §8 verbatim, §9 spend cap).

The money loop, against a real Postgres (RLS forced) + the hermetic provider:

- **confirm** path: customer confirms ⇒ one ``usage_records`` unit, same txn as the close.
- **silence** path: 72 h of silence after an answer ⇒ one unit (the beat sweep).
- **reopen / claw-back**: a reopen inside 72 h appends a ``-1`` (net 0); a reopen *after* the
  window leaves the resolution standing.
- **no double meter**: a redelivered confirm / re-run meters exactly once.
- **no human after**: a human reply after Neko's answer disqualifies the resolution.
- **spend cap**: past the monthly cap, the next turn routes to a human within one turn.
- **sandbox trace matches agent_runs**: the preview trace equals a real turn's retrieval trace.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy import func, select

from relay.core.db import session_scope
from relay.core.ids import IdPrefix, decode_public_id, encode_public_id
from relay.modules.ai import service as ai_service
from relay.modules.ai.models import AgentRun
from relay.modules.ai.pipeline import run_turn, sandbox_run
from relay.modules.ai.tasks import _scan_silence_resolutions
from relay.modules.billing import service as billing_service
from relay.modules.billing.models import UsageRecord
from relay.modules.knowledge.chunking import Chunk
from relay.modules.knowledge.embeddings import DeterministicEmbedder
from relay.modules.knowledge.indexing import index_chunks

pytestmark = pytest.mark.integration

PASSWORD = "correct-horse-battery-staple"


# --- helpers ------------------------------------------------------------------


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
    """Ingest an answer, open a conversation, let Neko answer it. Returns the conversation id."""
    await _ingest(ws, "Refunds are processed within 30 days for any subscription.", title="Refunds")
    conv, part = await _new_conversation(client, token, q)
    result = await run_turn(workspace_id=ws, conversation_id=conv, trigger_part_id=part)
    assert result.outcome == "answered", result
    return conv


async def _net_resolutions(ws: uuid.UUID) -> Decimal:
    async with session_scope(ws) as session:
        total = await session.scalar(
            select(func.coalesce(func.sum(UsageRecord.qty), 0)).where(
                UsageRecord.meter == billing_service.RESOLUTION_METER
            )
        )
    return Decimal(total or 0)


async def _usage_rows(ws: uuid.UUID) -> list[UsageRecord]:
    async with session_scope(ws) as session:
        rows = await session.scalars(
            select(UsageRecord)
            .where(UsageRecord.meter == billing_service.RESOLUTION_METER)
            .order_by(UsageRecord.created_at)
        )
        return list(rows.all())


async def _conv_state(client: httpx.AsyncClient, token: str, conv: uuid.UUID) -> dict:
    pub = encode_public_id(IdPrefix.CONVERSATION, conv)
    return dict((await client.get(f"/v0/conversations/{pub}", headers=_auth(token))).json())


async def _confirm(ws: uuid.UUID, conv: uuid.UUID) -> bool:
    async with session_scope(ws) as session:
        return await ai_service.confirm_resolution(session, workspace_id=ws, conversation_id=conv)


async def _reopen(client: httpx.AsyncClient, token: str, conv: uuid.UUID) -> None:
    pub = encode_public_id(IdPrefix.CONVERSATION, conv)
    r = await client.post(
        f"/v0/conversations/{pub}/state", json={"state": "open"}, headers=_auth(token)
    )
    assert r.status_code == 200, r.text


# --- confirm path -------------------------------------------------------------


async def test_confirm_meters_one_resolution(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "neko-confirm")
    await _enable_neko(client, token)
    conv = await _neko_answers(client, token, ws, "How do I get a refund?")

    assert await _confirm(ws, conv) is True
    rows = await _usage_rows(ws)
    assert len(rows) == 1 and rows[0].qty == 1
    # metered in the same txn as the close, keyed by the closing state_change part id
    conv_row = await _conv_state(client, token, conv)
    assert conv_row["state"] == "closed" and conv_row["ai_status"] == "resolved"


async def test_confirm_twice_is_idempotent(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "neko-confirm-idem")
    await _enable_neko(client, token)
    conv = await _neko_answers(client, token, ws, "How do I get a refund?")

    assert await _confirm(ws, conv) is True
    assert await _confirm(ws, conv) is False  # already closed — no second meter
    assert await _net_resolutions(ws) == Decimal(1)


async def test_human_reply_after_neko_disqualifies(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "neko-human-after")
    await _enable_neko(client, token)
    conv = await _neko_answers(client, token, ws, "How do I get a refund?")

    # A human teammate replies after Neko's answer (RFC-003 §8 clause 2 fails).
    pub = encode_public_id(IdPrefix.CONVERSATION, conv)
    r = await client.post(
        f"/v0/conversations/{pub}/reply",
        json={"body": "I'll take it from here."},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text

    assert await _confirm(ws, conv) is False
    assert await _net_resolutions(ws) == Decimal(0)


# --- silence path -------------------------------------------------------------


async def test_silence_sweep_meters_resolution(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "neko-silence")
    await _enable_neko(client, token)
    conv = await _neko_answers(client, token, ws, "How do I get a refund?")

    # Simulate 72 h of silence: backdate the head's last activity past the window.
    async with session_scope(ws) as session:
        await session.execute(
            sa.text(
                "UPDATE conversations SET last_part_at = now() - interval '73 hours' WHERE id = :c"
            ),
            {"c": conv},
        )

    metered = await _scan_silence_resolutions()
    assert metered >= 1
    assert await _net_resolutions(ws) == Decimal(1)
    assert (await _conv_state(client, token, conv))["state"] == "closed"


async def test_silence_requires_an_actual_answer(client: httpx.AsyncClient) -> None:
    """A clarifying question the customer ghosts is NOT a billable resolution (RFC-003 §8)."""
    token, ws = await _owner(client, "neko-silence-clarify")
    await _enable_neko(client, token)  # empty KB ⇒ Neko clarifies, never answers
    conv, part = await _new_conversation(client, token, "How do I configure SAML SSO?")
    first = await run_turn(workspace_id=ws, conversation_id=conv, trigger_part_id=part)
    assert first.outcome == "clarify"

    async with session_scope(ws) as session:
        await session.execute(
            sa.text(
                "UPDATE conversations SET last_part_at = now() - interval '73 hours' WHERE id = :c"
            ),
            {"c": conv},
        )
    await _scan_silence_resolutions()
    assert await _net_resolutions(ws) == Decimal(0)  # clarify + silence ≠ resolution


# --- reopen / claw-back -------------------------------------------------------


async def test_reopen_within_window_claws_back(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "neko-clawback")
    await _enable_neko(client, token)
    conv = await _neko_answers(client, token, ws, "How do I get a refund?")
    assert await _confirm(ws, conv) is True
    assert await _net_resolutions(ws) == Decimal(1)

    await _reopen(client, token, conv)  # reopened inside the 72 h window
    rows = await _usage_rows(ws)
    assert len(rows) == 2  # +1 then -1, appended (never mutated)
    assert sorted(r.qty for r in rows) == [-1, 1]
    assert await _net_resolutions(ws) == Decimal(0)


async def test_reopen_after_window_keeps_resolution(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "neko-clawback-late")
    await _enable_neko(client, token)
    conv = await _neko_answers(client, token, ws, "How do I get a refund?")
    assert await _confirm(ws, conv) is True

    # Age the closing state_change part past the 72 h claw-back window.
    async with session_scope(ws) as session:
        await session.execute(
            sa.text(
                "UPDATE conversation_parts SET created_at = created_at - interval '80 hours' "
                "WHERE conversation_id = :c AND part_type = 'state_change' "
                "AND meta->>'to' = 'closed'"
            ),
            {"c": conv},
        )

    await _reopen(client, token, conv)
    assert await _net_resolutions(ws) == Decimal(1)  # resolution stands; no claw-back


async def test_reopen_re_resolve_meters_again(client: httpx.AsyncClient) -> None:
    """A full resolve → reopen (claw-back) → re-resolve cycle nets one billable resolution."""
    token, ws = await _owner(client, "neko-recycle")
    await _enable_neko(client, token)
    conv = await _neko_answers(client, token, ws, "How do I get a refund?")
    assert await _confirm(ws, conv) is True
    await _reopen(client, token, conv)
    assert await _net_resolutions(ws) == Decimal(0)  # clawed back

    assert await _confirm(ws, conv) is True  # new resolution cycle, distinct close part
    assert await _net_resolutions(ws) == Decimal(1)


# --- spend cap ----------------------------------------------------------------


async def test_spend_cap_flips_routing_within_one_turn(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "neko-cap")
    # Cap at $1.00; at $0.99/resolution two recorded resolutions ($1.98) is over.
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
    assert (await _conv_state(client, token, conv))["ai_status"] == "handed_off"


async def test_under_cap_still_answers(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "neko-under-cap")
    await _enable_neko(client, token, monthly_spend_cap_usd=100.0)
    conv = await _neko_answers(client, token, ws, "How do I get a refund?")  # answers, not blocked
    assert (await _conv_state(client, token, conv))["ai_status"] == "active"


# --- product-surface HTTP endpoints -------------------------------------------


async def test_preview_and_usage_endpoints(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "neko-endpoints")
    await _enable_neko(client, token)
    await _ingest(ws, "Refunds are processed within 30 days.", title="Refunds")

    preview = await client.post(
        "/v0/ai/preview", json={"message": "How do I get a refund?"}, headers=_auth(token)
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["outcome"] == "answered"
    assert body["retrieved"] and "score" in body["retrieved"][0]  # trace visible

    usage = await client.get("/v0/ai/usage", headers=_auth(token))
    assert usage.status_code == 200, usage.text
    assert usage.json()["over_cap"] is False


# --- sandbox trace matches agent_runs -----------------------------------------


async def test_sandbox_trace_matches_agent_runs(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "neko-sandbox")
    await _enable_neko(client, token)
    await _ingest(ws, "Refunds are processed within 30 days for any subscription.", title="Refunds")

    # Real turn.
    conv, part = await _new_conversation(client, token, "How do I get a refund?")
    result = await run_turn(workspace_id=ws, conversation_id=conv, trigger_part_id=part)
    assert result.outcome == "answered"
    async with session_scope(ws) as session:
        run = (
            await session.scalars(select(AgentRun).where(AgentRun.conversation_id == conv))
        ).one()

    # Sandbox turn, same question against the same knowledge — persists nothing.
    record = await sandbox_run(workspace_id=ws, message="How do I get a refund?")

    assert record.outcome == run.outcome == "answered"
    assert record.retrieved == run.retrieved  # identical retrieval set (chunks + scores)
    assert record.prompt_hash == run.prompt_hash
    assert record.answer == run.answer
    # Sandbox left no ledger row / no extra usage.
    async with session_scope(ws) as session:
        assert await session.scalar(select(func.count()).select_from(AgentRun)) == 1
    assert await _net_resolutions(ws) == Decimal(0)
