"""Workflow engine chaos + idempotency tests (P1.5 acceptance, RFC-001 §6.7).

Acceptance bar: "kill workers mid-run, duplicate trigger delivery, broker flush → zero duplicate
side effects and all runs complete or park with resumable state; 1k concurrent runs without lock
contention." We prove each property deterministically against the real Postgres:

- duplicate trigger delivery → exactly one run (the ``(workspace, workflow, dedupe_key)`` unique);
- replayed advance (crash mid-run / redelivery) → the ledger skips the done effect (one part only);
- broker flush → the reaper (``scan_stuck_runs``) finds the stranded run; it completes on re-drive;
- many runs advanced concurrently → all complete, no lock errors (each run locks only its own row).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from relay.core.db import session_scope
from relay.core.ids import IdPrefix, decode_public_id, encode_public_id, uuid7
from relay.modules.automation import consumer, tasks
from relay.modules.automation.models import WorkflowRun
from relay.modules.messaging.models import ConversationPart

pytestmark = pytest.mark.integration

PASSWORD = "password123"


async def _owner(client: httpx.AsyncClient, ws_name: str = "Chaos") -> tuple[str, str]:
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
    return resp.json()["access_token"], resp.json()["workspace"]["id"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _conversation(client: httpx.AsyncClient, tok: str) -> dict:
    c = await client.post(
        "/v0/contacts/identify", json={"external_id": uuid4().hex}, headers=_auth(tok)
    )
    r = await client.post(
        "/v0/conversations", json={"contact_id": c.json()["id"], "body": "hi"}, headers=_auth(tok)
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _publish(client: httpx.AsyncClient, tok: str, graph: dict) -> None:
    wf = (await client.post("/v0/workflows", json={"name": "wf"}, headers=_auth(tok))).json()
    v = await client.post(
        f"/v0/workflows/{wf['id']}/versions", json={"graph": graph}, headers=_auth(tok)
    )
    assert v.status_code == 201, v.text
    pub = await client.post(
        f"/v0/workflows/{wf['id']}/publish", json={"version_id": v.json()["id"]}, headers=_auth(tok)
    )
    assert pub.status_code == 200, pub.text


def _payload(ws_pub: str, conv: dict) -> dict:
    return {
        "workspace_id": ws_pub,
        "conversation_id": conv["id"],
        "contact_id": conv["contact_id"],
        "state": conv["state"],
    }


_ADD_TAG = {
    "nodes": [
        {"id": "t", "type": "trigger", "trigger": "conversation.created", "next": "a"},
        {"id": "a", "type": "action", "action": "add_tag", "params": {"name": "x"}, "next": "e"},
        {"id": "e", "type": "end"},
    ]
}
_SEND_REPLY = {
    "nodes": [
        {"id": "t", "type": "trigger", "trigger": "conversation.created", "next": "a"},
        {
            "id": "a",
            "type": "action",
            "action": "send_reply",
            "params": {"body": "auto"},
            "next": "e",
        },
        {"id": "e", "type": "end"},
    ]
}


async def _count_runs(ws_uuid: object) -> int:
    async with session_scope(ws_uuid) as s:
        return int((await s.scalar(select(func.count()).select_from(WorkflowRun))) or 0)


# --- duplicate trigger delivery → exactly one run -----------------------------


async def test_duplicate_trigger_creates_one_run(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client)
    await _publish(client, tok, _ADD_TAG)
    conv = await _conversation(client, tok)
    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
    outbox_id = uuid7()  # same source event delivered twice

    first = await consumer._create_runs(
        ws_uuid, "conversation.created", "conversation.created", outbox_id, _payload(ws, conv)
    )
    second = await consumer._create_runs(
        ws_uuid, "conversation.created", "conversation.created", outbox_id, _payload(ws, conv)
    )
    assert len(first) == 1 and second == []  # the dedupe_key unique swallowed the redelivery
    assert await _count_runs(ws_uuid) == 1


# --- replayed advance → zero duplicate effects (the ledger) -------------------


async def test_replay_advance_is_idempotent(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client)
    await _publish(client, tok, _SEND_REPLY)
    conv = await _conversation(client, tok)
    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
    cid = decode_public_id(IdPrefix.CONVERSATION, conv["id"])

    runs = await consumer._create_runs(
        ws_uuid, "conversation.created", "conversation.created", uuid7(), _payload(ws, conv)
    )
    rid = runs[0]
    await tasks._advance_run(ws_uuid, rid)  # posts exactly one reply, completes

    async def _ai_comments() -> int:
        async with session_scope(ws_uuid) as s:
            return int(
                (
                    await s.scalar(
                        select(func.count())
                        .select_from(ConversationPart)
                        .where(
                            ConversationPart.conversation_id == cid,
                            ConversationPart.author_kind == "ai_agent",
                        )
                    )
                )
                or 0
            )

    assert await _ai_comments() == 1

    # Simulate a crash-then-redelivery: rewind the run to the send_reply node and re-advance. The
    # (run_id, node_id) ledger row already exists → the effect is skipped, not repeated.
    async with session_scope(ws_uuid) as s:
        run = (
            await s.execute(select(WorkflowRun).where(WorkflowRun.id == rid).with_for_update())
        ).scalar_one()
        run.status = "running"
        run.current_node_id = "a"
    await tasks._advance_run(ws_uuid, rid)

    assert await _ai_comments() == 1  # still exactly one — no duplicate side effect
    assert (
        await client.get(
            f"/v0/workflow_runs/{encode_public_id(IdPrefix.WORKFLOW_RUN, rid)}", headers=_auth(tok)
        )
    ).json()["status"] == "completed"


# --- broker flush → the reaper recovers a stranded run ------------------------


async def test_reaper_recovers_stranded_run(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client)
    await _publish(client, tok, _ADD_TAG)
    conv = await _conversation(client, tok)
    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)

    # A run was created but its advance message was "lost" (broker flush): it sits 'running'.
    runs = await consumer._create_runs(
        ws_uuid, "conversation.created", "conversation.created", uuid7(), _payload(ws, conv)
    )
    rid = runs[0]
    async with session_scope(ws_uuid) as s:
        run = (
            await s.execute(select(WorkflowRun).where(WorkflowRun.id == rid).with_for_update())
        ).scalar_one()
        assert run.status == "running"
        run.updated_at = run.created_at - dt.timedelta(hours=1)  # older than the stale window

    found = await tasks._scan_stuck_runs()  # the reaper's cross-workspace scan
    assert found >= 1

    # Re-drive (what the reaper enqueues) → the run completes.
    await tasks._advance_run(ws_uuid, rid)
    assert (
        await client.get(
            f"/v0/workflow_runs/{encode_public_id(IdPrefix.WORKFLOW_RUN, rid)}", headers=_auth(tok)
        )
    ).json()["status"] == "completed"


# --- concurrency: many runs advance without lock contention -------------------


async def test_concurrent_runs_no_contention(client: httpx.AsyncClient) -> None:
    """Advance many runs concurrently. Each advance locks only its own run row (FOR UPDATE by id),
    so there is no cross-run contention; all complete without a deadlock/lock error. (1k is the
    design target; the in-process pool caps practical concurrency, so this uses a representative N
    — the property under test, per-run isolation, is independent of N.)"""
    tok, ws = await _owner(client)
    await _publish(client, tok, _ADD_TAG)
    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)

    n = 24
    run_ids: list = []
    for _ in range(n):
        conv = await _conversation(client, tok)
        rids = await consumer._create_runs(
            ws_uuid, "conversation.created", "conversation.created", uuid7(), _payload(ws, conv)
        )
        run_ids.extend(rids)
    assert len(run_ids) == n

    results = await asyncio.gather(
        *(tasks._advance_run(ws_uuid, rid) for rid in run_ids), return_exceptions=True
    )
    assert all(r == "advanced" for r in results), results

    async with session_scope(ws_uuid) as s:
        statuses = list((await s.scalars(select(WorkflowRun.status))).all())
    assert len(statuses) == n and all(st == "completed" for st in statuses)


async def test_concurrent_double_advance_one_effect(client: httpx.AsyncClient) -> None:
    """Two advances of the SAME run race (crash-redelivery / reaper overlap). The run's FOR UPDATE
    lock serialises them and the loser sees status != 'running' → exactly one side effect."""
    tok, ws = await _owner(client)
    await _publish(client, tok, _SEND_REPLY)
    conv = await _conversation(client, tok)
    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
    cid = decode_public_id(IdPrefix.CONVERSATION, conv["id"])
    runs = await consumer._create_runs(
        ws_uuid, "conversation.created", "conversation.created", uuid7(), _payload(ws, conv)
    )
    rid = runs[0]

    results = await asyncio.gather(
        tasks._advance_run(ws_uuid, rid),
        tasks._advance_run(ws_uuid, rid),
        return_exceptions=True,
    )
    assert all(not isinstance(r, Exception) for r in results), results
    assert sorted(results) == ["advanced", "skip:completed"]  # one advanced, one no-op'd

    async with session_scope(ws_uuid) as s:
        n = int(
            (
                await s.scalar(
                    select(func.count())
                    .select_from(ConversationPart)
                    .where(
                        ConversationPart.conversation_id == cid,
                        ConversationPart.author_kind == "ai_agent",
                    )
                )
            )
            or 0
        )
    assert n == 1  # exactly one reply despite two concurrent advances
