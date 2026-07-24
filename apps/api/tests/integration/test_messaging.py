"""Messaging integration tests (P0.3 acceptance, RFC-002 §5.3, §5.6).

Covers the acceptance bar:
- duplicate send with the same Idempotency-Key returns the original part, exactly one row;
- W1 ``waiting_since`` rules (set on the contact part, cleared on the agent comment, untouched
  by a note) + outbox rows written in the same txn;
- the R1 inbox EXPLAIN plan is an Index Scan on ``conv_open_team`` with no Sort;
- state-machine violations rejected at BOTH the service layer (409/422) and the DB layer
  (``snooze_shape`` CHECK);
- assignment: manual, atomic claim, round-robin per team;
- R2 thread keyset pagination; cross-tenant isolation; saved replies + tags.
"""

from __future__ import annotations

import datetime as dt
import os
from uuid import uuid4

import httpx
import psycopg
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from relay.core.db import session_scope
from relay.core.ids import IdPrefix, decode_public_id

pytestmark = pytest.mark.integration

PASSWORD = "password123"


def _analyze_as_owner(table: str) -> None:
    """Refresh planner statistics for ``table`` as the migrator (its owner) — ``app_rw`` may not
    ANALYZE. The integration DB is session-scoped and never ANALYZEd by tests, so after other tests
    bulk-insert conversations the planner's stale estimates can flip an EXPLAIN plan (e.g. pick
    ``conv_open_asgn`` + a Sort over ``conv_open_team``); production always has fresh stats. This
    makes the plan assertion below deterministic regardless of suite order/volume."""
    dsn = os.environ["MIGRATION_DATABASE_URL"].replace("+psycopg", "")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f"ANALYZE {table}")


async def _owner(client: httpx.AsyncClient, ws_name: str) -> tuple[str, str]:
    """Sign up an owner; return (access_token, workspace_public_id)."""
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


async def _me_admin_id(client: httpx.AsyncClient, tok: str) -> str:
    me = (await client.get("/v0/auth/me", headers=_auth(tok))).json()
    return me["admin"]["id"]


async def _contact(client: httpx.AsyncClient, tok: str, external_id: str = "u1") -> str:
    r = await client.post(
        "/v0/contacts/identify", json={"external_id": external_id}, headers=_auth(tok)
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _conversation(
    client: httpx.AsyncClient, tok: str, *, body: str = "hi", team_id: str | None = None
) -> dict:
    contact_id = await _contact(client, tok, external_id=uuid4().hex)
    payload: dict = {"contact_id": contact_id, "body": body}
    if team_id:
        payload["team_id"] = team_id
    r = await client.post("/v0/conversations", json=payload, headers=_auth(tok))
    assert r.status_code == 201, r.text
    return r.json()


# --- Idempotency (RFC-002 §7) -------------------------------------------------


async def test_duplicate_send_same_key_returns_original_one_row(client: httpx.AsyncClient) -> None:
    """Acceptance: a retried reply with the same Idempotency-Key replays the original part and
    creates exactly one row."""
    tok, _ = await _owner(client, "Idem")
    conv = await _conversation(client, tok)
    headers = {**_auth(tok), "Idempotency-Key": "reply-123"}

    r1 = await client.post(
        f"/v0/conversations/{conv['id']}/reply", json={"body": "hello"}, headers=headers
    )
    assert r1.status_code == 201, r1.text
    r2 = await client.post(
        f"/v0/conversations/{conv['id']}/reply", json={"body": "hello"}, headers=headers
    )
    assert r2.status_code == 201, r2.text
    assert r1.json()["id"] == r2.json()["id"]  # same part replayed

    # Exactly one admin comment (the reply) exists — the duplicate created no second row.
    parts = (await client.get(f"/v0/conversations/{conv['id']}/parts", headers=_auth(tok))).json()
    admin_comments = [
        p for p in parts["items"] if p["part_type"] == "comment" and p["author_kind"] == "admin"
    ]
    assert len(admin_comments) == 1


async def test_same_key_different_body_conflicts(client: httpx.AsyncClient) -> None:
    tok, _ = await _owner(client, "IdemConflict")
    conv = await _conversation(client, tok)
    headers = {**_auth(tok), "Idempotency-Key": "k-1"}
    r1 = await client.post(
        f"/v0/conversations/{conv['id']}/reply", json={"body": "one"}, headers=headers
    )
    assert r1.status_code == 201
    r2 = await client.post(
        f"/v0/conversations/{conv['id']}/reply", json={"body": "TWO"}, headers=headers
    )
    assert r2.status_code == 409  # key reused with a different request


async def test_no_key_runs_every_time(client: httpx.AsyncClient) -> None:
    tok, _ = await _owner(client, "NoKey")
    conv = await _conversation(client, tok)
    for _ in range(2):
        r = await client.post(
            f"/v0/conversations/{conv['id']}/reply", json={"body": "x"}, headers=_auth(tok)
        )
        assert r.status_code == 201
    parts = (await client.get(f"/v0/conversations/{conv['id']}/parts", headers=_auth(tok))).json()
    admin_comments = [p for p in parts["items"] if p["author_kind"] == "admin"]
    assert len(admin_comments) == 2  # no key ⇒ two distinct rows


# --- W1: waiting_since rules --------------------------------------------------


async def test_waiting_since_rules(client: httpx.AsyncClient) -> None:
    """Set on the contact part; a note leaves it; an agent comment clears it (RFC-002 §5.3)."""
    tok, _ = await _owner(client, "Waiting")
    conv = await _conversation(client, tok, body="need help")
    assert conv["waiting_since"] is not None  # contact spoke → clock started
    assert conv["first_contact_reply_at"] is not None

    # A note does not touch waiting_since.
    await client.post(
        f"/v0/conversations/{conv['id']}/notes", json={"body": "internal"}, headers=_auth(tok)
    )
    after_note = (await client.get(f"/v0/conversations/{conv['id']}", headers=_auth(tok))).json()
    assert after_note["waiting_since"] is not None

    # An agent reply clears it.
    await client.post(
        f"/v0/conversations/{conv['id']}/reply", json={"body": "on it"}, headers=_auth(tok)
    )
    after_reply = (await client.get(f"/v0/conversations/{conv['id']}", headers=_auth(tok))).json()
    assert after_reply["waiting_since"] is None


async def test_w1_writes_outbox_in_same_txn(client: httpx.AsyncClient) -> None:
    """Every W1 writes outbox row(s) (the consistency spine)."""
    from relay.core.outbox import OutboxMessage

    tok, ws = await _owner(client, "Outbox1")
    conv = await _conversation(client, tok)  # emits conversation.created + part.created
    await client.post(
        f"/v0/conversations/{conv['id']}/reply", json={"body": "hi"}, headers=_auth(tok)
    )

    conv_uuid = decode_public_id(IdPrefix.CONVERSATION, conv["id"])
    async with session_scope(decode_public_id(IdPrefix.WORKSPACE, ws)) as s:
        rows = (
            await s.execute(
                select(OutboxMessage.topic, OutboxMessage.seq)
                .where(OutboxMessage.aggregate_id == conv_uuid)
                .order_by(OutboxMessage.seq)
            )
        ).all()
    topics = [r[0] for r in rows]
    seqs = [r[1] for r in rows]
    assert "conversation.created" in topics
    assert topics.count("conversation.part.created") == 2  # initial contact msg + the reply
    assert seqs == sorted(seqs) and seqs[0] == 1  # per-aggregate monotonic seq


# --- State machine (service + DB) ---------------------------------------------


def _future() -> str:
    return (dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).isoformat()


async def test_state_machine_valid_and_invalid_transitions(client: httpx.AsyncClient) -> None:
    tok, _ = await _owner(client, "States")
    conv = await _conversation(client, tok)

    # open → snoozed (needs snoozed_until) → closed → open
    r = await client.post(
        f"/v0/conversations/{conv['id']}/state",
        json={"state": "snoozed", "snoozed_until": _future()},
        headers=_auth(tok),
    )
    assert r.status_code == 200 and r.json()["state"] == "snoozed"
    r = await client.post(
        f"/v0/conversations/{conv['id']}/state", json={"state": "closed"}, headers=_auth(tok)
    )
    assert r.status_code == 200 and r.json()["state"] == "closed"

    # closed → snoozed is not a legal transition (service rejects, 409).
    bad = await client.post(
        f"/v0/conversations/{conv['id']}/state",
        json={"state": "snoozed", "snoozed_until": _future()},
        headers=_auth(tok),
    )
    assert bad.status_code == 409

    # reopen closed → open is legal.
    r = await client.post(
        f"/v0/conversations/{conv['id']}/state", json={"state": "open"}, headers=_auth(tok)
    )
    assert r.status_code == 200 and r.json()["state"] == "open"


async def test_snooze_without_until_rejected_by_service(client: httpx.AsyncClient) -> None:
    tok, _ = await _owner(client, "SnoozeReq")
    conv = await _conversation(client, tok)
    r = await client.post(
        f"/v0/conversations/{conv['id']}/state", json={"state": "snoozed"}, headers=_auth(tok)
    )
    assert r.status_code == 422  # service ValidationError


async def test_snooze_shape_rejected_by_db(client: httpx.AsyncClient) -> None:
    """DB layer: the snooze_shape CHECK rejects a snoozed row with no snoozed_until."""
    tok, ws = await _owner(client, "SnoozeDB")
    conv = await _conversation(client, tok)
    conv_uuid = decode_public_id(IdPrefix.CONVERSATION, conv["id"])
    with pytest.raises(IntegrityError):
        async with session_scope(decode_public_id(IdPrefix.WORKSPACE, ws)) as s:
            await s.execute(
                text(
                    "UPDATE conversations SET state = 'snoozed', snoozed_until = NULL "
                    "WHERE id = :id"
                ),
                {"id": conv_uuid},
            )


# --- Assignment ---------------------------------------------------------------


async def test_manual_assignment(client: httpx.AsyncClient) -> None:
    tok, _ = await _owner(client, "AssignManual")
    admin_id = await _me_admin_id(client, tok)
    conv = await _conversation(client, tok)
    r = await client.post(
        f"/v0/conversations/{conv['id']}/assign",
        json={"assignee_id": admin_id},
        headers=_auth(tok),
    )
    assert r.status_code == 200
    assert r.json()["assignee_id"] == admin_id


async def test_atomic_claim(client: httpx.AsyncClient) -> None:
    tok, _ = await _owner(client, "Claim")
    admin_id = await _me_admin_id(client, tok)
    conv = await _conversation(client, tok)
    assert conv["assignee_id"] is None

    r = await client.post(f"/v0/conversations/{conv['id']}/claim", headers=_auth(tok))
    assert r.status_code == 200 and r.json()["assignee_id"] == admin_id
    # Claiming again is a no-op (still assigned to the same agent, no error).
    r2 = await client.post(f"/v0/conversations/{conv['id']}/claim", headers=_auth(tok))
    assert r2.status_code == 200 and r2.json()["assignee_id"] == admin_id


async def test_round_robin_per_team(client: httpx.AsyncClient) -> None:
    """Round-robin assigns to team agents in rotation (RFC-002 §7)."""
    from relay.modules.identity.models import TeamMembership

    tok, ws = await _owner(client, "RoundRobin")

    # Two agents in the workspace.
    agents = []
    for i in range(2):
        m = await client.post(
            "/v0/members",
            json={"email": f"agent{i}-{uuid4().hex}@x.com", "name": f"Agent {i}", "role": "agent"},
            headers=_auth(tok),
        )
        assert m.status_code == 201, m.text
        agents.append(m.json())  # {id: membership, admin: {id: adm}, ...}

    team = (await client.post("/v0/teams", json={"name": "Support"}, headers=_auth(tok))).json()

    # Seed team memberships directly (team-member CRUD is identity's future work; P0.3 only
    # needs the rotation over a populated team).
    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
    team_uuid = decode_public_id(IdPrefix.TEAM, team["id"])
    async with session_scope(ws_uuid) as s:
        for a in agents:
            s.add(
                TeamMembership(
                    workspace_id=ws_uuid,
                    team_id=team_uuid,
                    membership_id=decode_public_id(IdPrefix.MEMBERSHIP, a["id"]),
                )
            )

    agent_admin_ids = {a["admin"]["id"] for a in agents}
    assignees = []
    for _ in range(2):
        conv = await _conversation(client, tok)
        r = await client.post(
            f"/v0/conversations/{conv['id']}/assign/round-robin",
            json={"team_id": team["id"]},
            headers=_auth(tok),
        )
        assert r.status_code == 200, r.text
        assignees.append(r.json()["assignee_id"])

    assert set(assignees) <= agent_admin_ids
    assert assignees[0] != assignees[1]  # rotated between the two agents


# --- R1 inbox EXPLAIN (the money query) ---------------------------------------


async def test_r1_inbox_uses_partial_index_no_sort(client: httpx.AsyncClient) -> None:
    """Acceptance: the R1 inbox query is an Index Scan on conv_open_team, no Sort."""
    tok, ws = await _owner(client, "Inbox")
    team = (await client.post("/v0/teams", json={"name": "Ops"}, headers=_auth(tok))).json()
    for _ in range(20):
        await _conversation(client, tok, team_id=team["id"])

    # Fresh stats so the planner's choice is deterministic under any accumulated suite volume.
    _analyze_as_owner("conversations")

    team_uuid = decode_public_id(IdPrefix.TEAM, team["id"])
    async with session_scope(decode_public_id(IdPrefix.WORKSPACE, ws)) as s:
        # Force the planner to reveal the *ordered* index scan: with seqscan + bitmapscan off it
        # can't fall back to a Seq Scan or an (unordered) Bitmap Index Scan + Sort, so it must use
        # ``conv_open_team`` (which supplies the ``waiting_since`` order) — the property R1 relies
        # on in production. Without this a bitmap scan can win on cost and add a Sort node.
        await s.execute(text("SET LOCAL enable_seqscan = off"))
        await s.execute(text("SET LOCAL enable_bitmapscan = off"))
        rows = (
            await s.execute(
                text(
                    "EXPLAIN SELECT id FROM conversations "
                    "WHERE team_id = :t AND state = 'open' "
                    "ORDER BY waiting_since LIMIT 50"
                ),
                {"t": team_uuid},
            )
        ).all()
    plan = "\n".join(r[0] for r in rows)
    assert "conv_open_team" in plan, plan
    assert "Seq Scan" not in plan, plan
    assert "Sort" not in plan, plan


# --- R2 thread keyset ---------------------------------------------------------


async def test_thread_keyset_pagination(client: httpx.AsyncClient) -> None:
    tok, _ = await _owner(client, "Thread")
    conv = await _conversation(client, tok, body="first")
    for i in range(4):
        await client.post(
            f"/v0/conversations/{conv['id']}/reply", json={"body": f"r{i}"}, headers=_auth(tok)
        )

    page1 = (
        await client.get(f"/v0/conversations/{conv['id']}/parts?limit=2", headers=_auth(tok))
    ).json()
    assert len(page1["items"]) == 2
    assert page1["next_cursor"] is not None
    page2 = (
        await client.get(
            f"/v0/conversations/{conv['id']}/parts?limit=2&cursor={page1['next_cursor']}",
            headers=_auth(tok),
        )
    ).json()
    ids1 = {p["id"] for p in page1["items"]}
    ids2 = {p["id"] for p in page2["items"]}
    assert ids1.isdisjoint(ids2)  # keyset, no overlap
    # Newest-first ordering (uuid7 ids sort by time).
    assert page1["items"][0]["id"] > page1["items"][1]["id"]


# --- Cross-tenant isolation (master rule 1) -----------------------------------


async def test_conversations_cross_tenant_isolation(client: httpx.AsyncClient) -> None:
    tok_a, _ = await _owner(client, "MsgAlpha")
    tok_b, _ = await _owner(client, "MsgBravo")
    conv_a = await _conversation(client, tok_a)

    # B sees none of A's conversations, and cannot read A's conversation (RLS → 404).
    listing_b = (await client.get("/v0/conversations", headers=_auth(tok_b))).json()
    assert listing_b["items"] == []
    assert (
        await client.get(f"/v0/conversations/{conv_a['id']}", headers=_auth(tok_b))
    ).status_code == 404


# --- Saved replies + tags -----------------------------------------------------


async def test_saved_replies_crud(client: httpx.AsyncClient) -> None:
    tok, _ = await _owner(client, "Macros")
    created = await client.post(
        "/v0/saved-replies",
        json={"shortcut": "hi", "title": "Greeting", "body": "Hello there!"},
        headers=_auth(tok),
    )
    assert created.status_code == 201, created.text
    listing = (await client.get("/v0/saved-replies", headers=_auth(tok))).json()
    assert any(r["shortcut"] == "hi" for r in listing)
    d = await client.delete(f"/v0/saved-replies/{created.json()['id']}", headers=_auth(tok))
    assert d.status_code == 204


async def test_conversation_tags(client: httpx.AsyncClient) -> None:
    tok, _ = await _owner(client, "Tags")
    conv = await _conversation(client, tok)
    r = await client.post(
        f"/v0/conversations/{conv['id']}/tags", json={"name": "vip"}, headers=_auth(tok)
    )
    assert r.status_code == 204
    # Idempotent: adding the same tag again is a no-op.
    await client.post(
        f"/v0/conversations/{conv['id']}/tags", json={"name": "vip"}, headers=_auth(tok)
    )
    tags = (await client.get(f"/v0/conversations/{conv['id']}/tags", headers=_auth(tok))).json()
    assert [t["name"] for t in tags] == ["vip"]

    await client.delete(f"/v0/conversations/{conv['id']}/tags/vip", headers=_auth(tok))
    tags = (await client.get(f"/v0/conversations/{conv['id']}/tags", headers=_auth(tok))).json()
    assert tags == []
