"""Neko orchestrator integration tests (P1.2 acceptance, RFC-003 §3/§5/§6/§8).

Exercises the whole turn pipeline against a real Postgres (RLS forced) + the hermetic provider:
grounded answer + ledger + ai_status; instant "talk to a person" handoff; grounding-gate clarify →
handoff; provider-blackhole failover mid-turn with no user-visible error; verifier rejects a planted
ungrounded claim; idempotent double-trigger; per-workspace + global kill switches; agent_runs RLS;
and the replay tool reproducing a turn from ``agent_runs``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from relay.core.db import get_sessionmaker, session_scope
from relay.core.ids import IdPrefix, decode_public_id, encode_public_id
from relay.modules.ai import protocol
from relay.modules.ai.models import AgentRun
from relay.modules.ai.pipeline import run_turn
from relay.modules.ai.providers import DeterministicProvider, LLMResponse, LLMTimeout, StreamChunk
from relay.modules.ai.resilience import LLMRouter, ProviderRoute
from relay.modules.knowledge.chunking import Chunk
from relay.modules.knowledge.embeddings import DeterministicEmbedder
from relay.modules.knowledge.indexing import index_chunks
from relay.settings import get_settings

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


def _conv_pub(conv: uuid.UUID) -> str:
    return encode_public_id(IdPrefix.CONVERSATION, conv)


async def _enable_neko(client: httpx.AsyncClient, token: str, **overrides: object) -> None:
    payload: dict[str, object] = {"enabled": True, "channels": ["chat"]}
    payload.update(overrides)
    r = await client.patch("/v0/ai/settings", json=payload, headers=_auth(token))
    assert r.status_code == 200, r.text


async def _ingest(ws: uuid.UUID, content: str, *, title: str | None = None) -> uuid.UUID:
    source_id = uuid.uuid4()
    async with session_scope(ws) as session:
        await index_chunks(
            session,
            workspace_id=ws,
            source_kind="article",
            source_id=source_id,
            locale="en",
            audience={},
            title=title,
            chunks=[Chunk(chunk_index=0, content=content, heading_path=None, token_count=10)],
            embedder=DeterministicEmbedder(),
            emb_version=1,
        )
    return source_id


async def _new_conversation(
    client: httpx.AsyncClient, token: str, body: str
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a conversation whose first part is the customer message. Returns (conv_id, part)."""
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


async def _runs(ws: uuid.UUID, conv: uuid.UUID) -> list[AgentRun]:
    async with session_scope(ws) as session:
        return list(
            (
                await session.scalars(
                    select(AgentRun)
                    .where(AgentRun.conversation_id == conv)
                    .order_by(AgentRun.id.asc())
                )
            ).all()
        )


async def _parts(client: httpx.AsyncClient, token: str, conv: uuid.UUID) -> list[dict]:
    r = await client.get(f"/v0/conversations/{_conv_pub(conv)}/parts", headers=_auth(token))
    return list(r.json()["items"])


async def _conversation(client: httpx.AsyncClient, token: str, conv: uuid.UUID) -> dict:
    return dict(
        (await client.get(f"/v0/conversations/{_conv_pub(conv)}", headers=_auth(token))).json()
    )


def _capture() -> tuple[list[tuple[str, dict]], Callable[[str, dict], Awaitable[None]]]:
    events: list[tuple[str, dict]] = []

    async def publish(channel: str, data: dict) -> None:
        events.append((channel, data))

    return events, publish


# --- tests --------------------------------------------------------------------


async def test_turn_answers_grounded_question(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "neko-answer")
    await _enable_neko(client, token)
    await _ingest(ws, "Refunds are processed within 30 days for any subscription.", title="Refunds")
    conv, part = await _new_conversation(client, token, "How do I get a refund?")

    events, publish = _capture()
    result = await run_turn(
        workspace_id=ws, conversation_id=conv, trigger_part_id=part, stream_publish=publish
    )
    assert result.outcome == "answered", result

    runs = await _runs(ws, conv)
    assert len(runs) == 1
    run = runs[0]
    assert run.status == "complete" and run.outcome == "answered"
    assert run.retrieved and run.citations  # cited its evidence (RFC-003 §6)
    assert run.prompt_hash and run.cost_usd > 0
    assert run.latency_ms.get("first_token") is not None  # first-token latency recorded
    assert run.models.get("generate") and run.models.get("verify")

    parts = await _parts(client, token, conv)
    ai_comments = [
        p for p in parts if p["author_kind"] == "ai_agent" and p["part_type"] == "comment"
    ]
    assert len(ai_comments) == 1
    assert "30 days" in (ai_comments[0]["body"] or "")
    assert (await _conversation(client, token, conv))["ai_status"] == "active"

    topics = {d["topic"] for _c, d in events}
    assert "ai.stream.start" in topics and "ai.stream.end" in topics


async def test_talk_to_a_person_hands_off_instantly(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "neko-handoff")
    await _enable_neko(client, token)
    await _ingest(ws, "Refunds are processed within 30 days.", title="Refunds")
    conv, part = await _new_conversation(client, token, "Please let me talk to a person")

    result = await run_turn(workspace_id=ws, conversation_id=conv, trigger_part_id=part)
    assert result.outcome == "handoff" and result.handoff_reason == "explicit_request"

    run = (await _runs(ws, conv))[0]
    assert run.outcome == "handoff"
    assert not run.prompt_hash  # never reached generation

    parts = await _parts(client, token, conv)
    assert any(p["part_type"] == "note" and p["author_kind"] == "ai_agent" for p in parts)  # recap
    assert (await _conversation(client, token, conv))["ai_status"] == "handed_off"


async def test_grounding_gate_clarifies_then_hands_off(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "neko-clarify")
    await _enable_neko(client, token)  # empty knowledge base ⇒ nothing grounds
    conv, part = await _new_conversation(client, token, "How do I configure SAML SSO provisioning?")

    first = await run_turn(workspace_id=ws, conversation_id=conv, trigger_part_id=part)
    assert first.outcome == "clarify", first

    # A second unanswerable turn exhausts the one-clarification budget ⇒ handoff (never loops).
    second = await run_turn(workspace_id=ws, conversation_id=conv, trigger_part_id=uuid.uuid4())
    assert second.outcome == "handoff" and second.handoff_reason == "insufficient_grounding"


async def test_disabled_workspace_is_ineligible(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "neko-off")
    conv, part = await _new_conversation(
        client, token, "How do I get a refund?"
    )  # Neko off (default)
    result = await run_turn(workspace_id=ws, conversation_id=conv, trigger_part_id=part)
    assert result.outcome == "ineligible" and result.reason == "workspace_disabled"
    async with session_scope(ws) as session:
        n = await session.scalar(select(func.count()).select_from(AgentRun))
    assert n == 0  # no ledger row for a non-turn


async def test_global_kill_switch(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "neko-global-off")
    await _enable_neko(client, token)
    conv, part = await _new_conversation(client, token, "How do I get a refund?")
    off = get_settings().model_copy(update={"ai_model_route": "off"})
    result = await run_turn(
        workspace_id=ws, conversation_id=conv, trigger_part_id=part, settings=off
    )
    assert result.outcome == "ineligible" and result.reason == "global_off"


async def test_double_trigger_is_idempotent(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "neko-idem")
    await _enable_neko(client, token)
    await _ingest(ws, "Refunds are processed within 30 days.", title="Refunds")
    conv, part = await _new_conversation(client, token, "How do I get a refund?")

    a = await run_turn(workspace_id=ws, conversation_id=conv, trigger_part_id=part)
    b = await run_turn(workspace_id=ws, conversation_id=conv, trigger_part_id=part)
    assert a.outcome == "answered"
    assert b.outcome == "ineligible" and b.reason == "already_processed"  # claim gate

    assert len(await _runs(ws, conv)) == 1  # exactly one ledger row
    parts = await _parts(client, token, conv)
    ai_comments = [
        p for p in parts if p["author_kind"] == "ai_agent" and p["part_type"] == "comment"
    ]
    assert len(ai_comments) == 1  # never double-answered


async def test_provider_blackhole_fails_over_mid_turn(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "neko-failover")
    await _enable_neko(client, token)
    await _ingest(ws, "Refunds are processed within 30 days.", title="Refunds")
    conv, part = await _new_conversation(client, token, "How do I get a refund?")

    router = LLMRouter(
        [
            ProviderRoute(provider=_Blackhole(), cheap_model="c", frontier_model="f"),
            ProviderRoute(provider=DeterministicProvider(), cheap_model="c", frontier_model="f"),
        ]
    )
    result = await run_turn(
        workspace_id=ws, conversation_id=conv, trigger_part_id=part, router=router
    )
    assert result.outcome == "answered"  # failed over without a user-visible error
    assert (await _runs(ws, conv))[0].provider == "deterministic"  # secondary served generation
    parts = await _parts(client, token, conv)
    assert any(p["author_kind"] == "ai_agent" and p["part_type"] == "comment" for p in parts)


async def test_verifier_rejects_planted_ungrounded_claim(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "neko-verify")
    await _enable_neko(client, token)
    await _ingest(ws, "Orders ship within three days of purchase.", title="Shipping")
    conv, part = await _new_conversation(client, token, "When does my order ship?")

    router = LLMRouter(
        [ProviderRoute(provider=_UngroundedGenerator(), cheap_model="c", frontier_model="f")]
    )
    result = await run_turn(
        workspace_id=ws, conversation_id=conv, trigger_part_id=part, router=router
    )
    assert result.outcome == "handoff" and result.handoff_reason == "verify_reject"

    assert (await _runs(ws, conv))[0].verdict.get("grounded") is False  # verifier caught it
    # The ungrounded draft was NOT persisted as an answer.
    bodies = [p["body"] or "" for p in await _parts(client, token, conv)]
    assert not any("unicorn" in b for b in bodies)


async def test_replay_reproduces_turn(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "neko-replay")
    await _enable_neko(client, token)
    await _ingest(ws, "Refunds are processed within 30 days.", title="Refunds")
    conv, part = await _new_conversation(client, token, "How do I get a refund?")
    result = await run_turn(workspace_id=ws, conversation_id=conv, trigger_part_id=part)
    assert result.outcome == "answered" and result.run_id is not None

    run_pub = encode_public_id(IdPrefix.AGENT_RUN, result.run_id)
    r = await client.post(f"/v0/ai/runs/{run_pub}/replay", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reproducible"] is True
    assert body["prompt_hash_match"] is True and body["answer_match"] is True


async def test_agent_runs_are_workspace_isolated(client: httpx.AsyncClient) -> None:
    token_a, ws_a = await _owner(client, "neko-rls-a")
    _token_b, ws_b = await _owner(client, "neko-rls-b")
    await _enable_neko(client, token_a)
    await _ingest(ws_a, "Refunds are processed within 30 days.", title="Refunds")
    conv, part = await _new_conversation(client, token_a, "How do I get a refund?")
    await run_turn(workspace_id=ws_a, conversation_id=conv, trigger_part_id=part)

    # Workspace B's session sees zero of A's agent_runs (RLS forced).
    async with session_scope(ws_b) as session:
        assert await session.scalar(select(func.count()).select_from(AgentRun)) == 0
    # And an unset GUC returns zero rows (defence-in-depth backstop).
    async with get_sessionmaker()() as session, session.begin():
        assert await session.scalar(select(func.count()).select_from(AgentRun)) == 0


# --- test providers -----------------------------------------------------------


class _Blackhole:
    name = "blackhole"

    async def complete(self, **_kw: object) -> LLMResponse:
        raise LLMTimeout("blackhole")

    async def stream(self, **_kw: object) -> AsyncIterator[StreamChunk]:
        raise LLMTimeout("blackhole")
        yield StreamChunk()  # pragma: no cover — makes this an async generator


class _UngroundedGenerator(DeterministicProvider):
    """Preflight/rewrite/verify behave normally; generation plants an ungrounded claim."""

    def _generate(self, by_label: dict[str, tuple[dict[str, str], str]], *, max_tokens: int) -> str:
        label = next((lbl for lbl in by_label if lbl.startswith("c")), "c1")
        return f"Your order will arrive by unicorn express tomorrow. {protocol.cite(label)}"
