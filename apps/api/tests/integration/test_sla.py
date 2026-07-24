"""Integration tests for the SLA engine (P1.7 S2).

Covers policy CRUD + validation, applying a policy, the event-driven clock (first-response met,
resolution met, reopen claw-back), business-hours due computation (the weekend fixture, via a real
stored schedule), and the durable breach sweep — including **exactly-once** firing under a sweep
re-run (the chaos requirement). Breach timing is made deterministic by pushing due-times into the
past directly, so no test sleeps.
"""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from relay.core.db import session_scope
from relay.core.ids import IdPrefix, decode_public_id
from relay.core.outbox import OutboxMessage
from relay.modules.messaging import events, sla
from relay.modules.messaging.models import ConversationSla, SlaEvent, SlaPolicy

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"
_UTC = dt.UTC


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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


async def _contact(client: httpx.AsyncClient, tok: str) -> str:
    r = await client.post(
        "/v0/contacts/identify", json={"external_id": uuid4().hex}, headers=_auth(tok)
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _conversation(client: httpx.AsyncClient, tok: str) -> dict:
    contact_id = await _contact(client, tok)
    r = await client.post(
        "/v0/conversations", json={"contact_id": contact_id, "body": "hi"}, headers=_auth(tok)
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _policy(client: httpx.AsyncClient, tok: str, **overrides: object) -> dict:
    body: dict = {"name": "Default SLA", "first_response_seconds": 3600}
    body.update(overrides)
    r = await client.post("/v0/sla-policies", json=body, headers=_auth(tok))
    assert r.status_code == 201, r.text
    return r.json()


def _ws_uuid(ws_pub: str) -> object:
    return decode_public_id(IdPrefix.WORKSPACE, ws_pub)


# --- policy CRUD + validation -------------------------------------------------


async def test_policy_crud(client: httpx.AsyncClient) -> None:
    tok, _ws = await _owner(client, "SlaCrud")
    created = await _policy(
        client, tok, name="Gold", first_response_seconds=1800, resolution_seconds=86400
    )
    assert created["first_response_seconds"] == 1800
    assert created["resolution_seconds"] == 86400

    listing = (await client.get("/v0/sla-policies", headers=_auth(tok))).json()
    assert len(listing) == 1

    upd = await client.put(
        f"/v0/sla-policies/{created['id']}",
        json={"name": "Gold+", "first_response_seconds": 900},
        headers=_auth(tok),
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["name"] == "Gold+"
    assert upd.json()["resolution_seconds"] is None

    d = await client.delete(f"/v0/sla-policies/{created['id']}", headers=_auth(tok))
    assert d.status_code == 204
    assert (await client.get("/v0/sla-policies", headers=_auth(tok))).json() == []


async def test_policy_requires_a_target(client: httpx.AsyncClient) -> None:
    tok, _ws = await _owner(client, "SlaNoTarget")
    r = await client.post("/v0/sla-policies", json={"name": "empty"}, headers=_auth(tok))
    assert r.status_code == 422, r.text


async def test_policy_rejects_bad_predicate(client: httpx.AsyncClient) -> None:
    tok, _ws = await _owner(client, "SlaBadPred")
    r = await client.post(
        "/v0/sla-policies",
        json={"name": "p", "first_response_seconds": 60, "apply_predicate": {"op": "bogus"}},
        headers=_auth(tok),
    )
    assert r.status_code == 422, r.text


# --- apply + read -------------------------------------------------------------


async def test_apply_arms_due_times(client: httpx.AsyncClient) -> None:
    tok, _ws = await _owner(client, "SlaApply")
    policy = await _policy(client, tok, first_response_seconds=3600, resolution_seconds=7200)
    conv = await _conversation(client, tok)
    r = await client.post(
        f"/v0/conversations/{conv['id']}/sla",
        json={"policy_id": policy["id"]},
        headers=_auth(tok),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["first_response"]["due_at"] is not None
    assert body["resolution"]["due_at"] is not None
    assert body["next_response"]["due_at"] is None
    assert body["active"] is True

    got = (await client.get(f"/v0/conversations/{conv['id']}/sla", headers=_auth(tok))).json()
    assert got["policy_id"] == policy["id"]

    # Remove it → 404 afterwards.
    assert (
        await client.delete(f"/v0/conversations/{conv['id']}/sla", headers=_auth(tok))
    ).status_code == 204
    assert (
        await client.get(f"/v0/conversations/{conv['id']}/sla", headers=_auth(tok))
    ).status_code == 404


async def test_apply_inactive_policy_conflicts(client: httpx.AsyncClient) -> None:
    tok, _ws = await _owner(client, "SlaInactive")
    policy = await _policy(client, tok, active=False, first_response_seconds=60)
    conv = await _conversation(client, tok)
    r = await client.post(
        f"/v0/conversations/{conv['id']}/sla",
        json={"policy_id": policy["id"]},
        headers=_auth(tok),
    )
    assert r.status_code == 409, r.text


# --- the clock fold (driven directly, as the sla_consumer would) --------------


async def _fold(ws_pub: str, conv_pub: str, topic: str, payload: dict, seq: int) -> None:
    """Apply one conversation event to the SLA clock, as ``sla_consumer._apply_to_db`` would."""
    cid = decode_public_id(IdPrefix.CONVERSATION, conv_pub)
    async with session_scope(_ws_uuid(ws_pub)) as session:
        row = (
            await session.execute(
                select(ConversationSla)
                .where(ConversationSla.conversation_id == cid)
                .with_for_update()
            )
        ).scalar_one()
        policy = await session.get(SlaPolicy, row.policy_id)
        assert policy is not None
        await sla.apply_conversation_event(session, row, policy, topic, payload)
        row.last_seq = seq


def _part_payload(ws_pub: str, conv_pub: str, *, author_kind: str, at: dt.datetime) -> dict:
    return {
        "workspace_id": ws_pub,
        "conversation_id": conv_pub,
        "part_type": "comment",
        "author_kind": author_kind,
        "created_at": at.isoformat(),
    }


async def test_first_response_met_by_agent_reply(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client, "SlaFR")
    policy = await _policy(client, tok, first_response_seconds=3600)
    conv = await _conversation(client, tok)
    await client.post(
        f"/v0/conversations/{conv['id']}/sla", json={"policy_id": policy["id"]}, headers=_auth(tok)
    )
    # An agent reply satisfies first-response.
    now = dt.datetime.now(_UTC)
    await _fold(
        ws,
        conv["id"],
        events.CONVERSATION_PART_CREATED,
        _part_payload(ws, conv["id"], author_kind="admin", at=now),
        seq=10,
    )

    got = (await client.get(f"/v0/conversations/{conv['id']}/sla", headers=_auth(tok))).json()
    assert got["first_response"]["satisfied_at"] is not None
    assert got["first_response"]["breached_at"] is None
    assert got["next_breach_at"] is None  # nothing else pending
    assert got["active"] is False


async def test_reopen_claws_back_resolution(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client, "SlaClawback")
    policy = await _policy(client, tok, first_response_seconds=None, resolution_seconds=3600)
    conv = await _conversation(client, tok)
    await client.post(
        f"/v0/conversations/{conv['id']}/sla", json={"policy_id": policy["id"]}, headers=_auth(tok)
    )
    now = dt.datetime.now(_UTC)
    # Close → resolution satisfied.
    await _fold(
        ws,
        conv["id"],
        events.CONVERSATION_STATE_CHANGED,
        {
            "workspace_id": ws,
            "conversation_id": conv["id"],
            "to": "closed",
            "occurred_at": now.isoformat(),
        },
        seq=10,
    )
    got = (await client.get(f"/v0/conversations/{conv['id']}/sla", headers=_auth(tok))).json()
    assert got["resolution"]["satisfied_at"] is not None

    # Reopen → resolution re-armed (claw-back): satisfied cleared, a fresh due set.
    later = now + dt.timedelta(minutes=1)
    await _fold(
        ws,
        conv["id"],
        events.CONVERSATION_STATE_CHANGED,
        {
            "workspace_id": ws,
            "conversation_id": conv["id"],
            "to": "open",
            "occurred_at": later.isoformat(),
        },
        seq=11,
    )
    got = (await client.get(f"/v0/conversations/{conv['id']}/sla", headers=_auth(tok))).json()
    assert got["resolution"]["satisfied_at"] is None
    assert got["resolution"]["due_at"] is not None
    assert got["active"] is True


# --- business-hours due computation (ties S2 apply to the S1 weekend fixture) --


async def test_business_hours_due_uses_the_weekend_fixture(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client, "SlaBH")
    await client.put(
        "/v0/office-hours",
        json={
            "timezone": "UTC",
            "weekly": {str(d): [{"open": "09:00", "close": "17:00"}] for d in range(5)},
            "holidays": [],
        },
        headers=_auth(tok),
    )
    # A 2h business-hours budget from Friday 16:00 lands Monday 10:00 (weekend skipped).
    start = dt.datetime(2021, 1, 1, 16, 0, tzinfo=_UTC)  # Friday
    async with session_scope(_ws_uuid(ws)) as session:
        due = await sla._due_at(
            session, business_hours=True, team_id=None, start=start, seconds=2 * 3600
        )
    assert due == dt.datetime(2021, 1, 4, 10, 0, tzinfo=_UTC)


# --- auto-apply rule ----------------------------------------------------------


async def test_rule_auto_apply_on_new_conversation(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client, "SlaRule")
    policy = await _policy(
        client,
        tok,
        first_response_seconds=3600,
        apply_predicate={"op": "eq", "field": "channel", "value": "chat"},
    )
    conv = await _conversation(client, tok)
    # The sla_consumer runs maybe_auto_apply on conversation.created; drive it directly here.
    async with session_scope(_ws_uuid(ws)) as session:
        applied = await sla.maybe_auto_apply(
            session, decode_public_id(IdPrefix.CONVERSATION, conv["id"])
        )
    assert applied is True
    got = (await client.get(f"/v0/conversations/{conv['id']}/sla", headers=_auth(tok))).json()
    assert got["policy_id"] == policy["id"]


# --- durable breach + exactly-once (the chaos requirement) --------------------


async def _force_due_in_past(ws_pub: str, conv_pub: str) -> None:
    """Simulate the first-response clock elapsing without sleeping."""
    cid = decode_public_id(IdPrefix.CONVERSATION, conv_pub)
    past = dt.datetime.now(_UTC) - dt.timedelta(minutes=5)
    async with session_scope(_ws_uuid(ws_pub)) as session:
        row = (
            await session.execute(
                select(ConversationSla)
                .where(ConversationSla.conversation_id == cid)
                .with_for_update()
            )
        ).scalar_one()
        row.first_response_due_at = past
        row.next_breach_at = past


async def _breached_event_count(ws_pub: str, conv_pub: str) -> int:
    cid = decode_public_id(IdPrefix.CONVERSATION, conv_pub)
    async with session_scope(_ws_uuid(ws_pub)) as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(SlaEvent)
                .where(SlaEvent.conversation_id == cid, SlaEvent.kind == "breached")
            )
        ).scalar_one()


async def test_breach_sweep_fires_escalation_and_is_exactly_once(
    client: httpx.AsyncClient,
) -> None:
    tok, ws = await _owner(client, "SlaBreach")
    policy = await _policy(
        client, tok, first_response_seconds=3600, escalation={"set_priority": True, "notify": True}
    )
    conv = await _conversation(client, tok)
    await client.post(
        f"/v0/conversations/{conv['id']}/sla", json={"policy_id": policy["id"]}, headers=_auth(tok)
    )
    await _force_due_in_past(ws, conv["id"])

    breached = await sla.sweep_due_breaches()
    assert breached == 1

    got = (await client.get(f"/v0/conversations/{conv['id']}/sla", headers=_auth(tok))).json()
    assert got["first_response"]["breached_at"] is not None
    assert got["active"] is False  # nothing else pending

    # Escalation applied: priority set on the conversation head.
    convrow = (await client.get(f"/v0/conversations/{conv['id']}", headers=_auth(tok))).json()
    assert convrow["priority"] is True

    # A breach outbox event was emitted (relay isn't running in tests, so the row persists).
    cid = decode_public_id(IdPrefix.CONVERSATION, conv["id"])
    async with session_scope(_ws_uuid(ws)) as session:
        n_events = (
            await session.execute(
                select(func.count())
                .select_from(OutboxMessage)
                .where(
                    OutboxMessage.aggregate_id == cid,
                    OutboxMessage.topic == events.CONVERSATION_SLA_BREACHED,
                )
            )
        ).scalar_one()
    assert n_events == 1
    assert await _breached_event_count(ws, conv["id"]) == 1

    # Re-run the sweep (crash/lease-reclaim/redelivery): must NOT double-fire.
    assert await sla.sweep_due_breaches() == 0
    assert await _breached_event_count(ws, conv["id"]) == 1


# --- next_response lifecycle (regression: a follow-up must not push the deadline) --------------


async def _get_sla(client: httpx.AsyncClient, tok: str, conv_id: str) -> dict:
    return (await client.get(f"/v0/conversations/{conv_id}/sla", headers=_auth(tok))).json()


async def _team(client: httpx.AsyncClient, tok: str, name: str = "Support") -> dict:
    r = await client.post("/v0/teams", json={"name": name}, headers=_auth(tok))
    assert r.status_code == 201, r.text
    return r.json()


async def test_next_response_arms_once_and_is_satisfied(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client, "SlaNext")
    policy = await _policy(client, tok, first_response_seconds=3600, next_response_seconds=1800)
    conv = await _conversation(client, tok)
    await client.post(
        f"/v0/conversations/{conv['id']}/sla", json={"policy_id": policy["id"]}, headers=_auth(tok)
    )
    now = dt.datetime.now(_UTC)

    # Agent reply satisfies first-response; next-response is not armed yet.
    await _fold(
        ws,
        conv["id"],
        events.CONVERSATION_PART_CREATED,
        _part_payload(ws, conv["id"], author_kind="admin", at=now),
        seq=10,
    )
    got = await _get_sla(client, tok, conv["id"])
    assert got["first_response"]["satisfied_at"] is not None
    assert got["next_response"]["due_at"] is None

    # A contact follow-up arms next-response.
    await _fold(
        ws,
        conv["id"],
        events.CONVERSATION_PART_CREATED,
        _part_payload(ws, conv["id"], author_kind="contact", at=now + dt.timedelta(minutes=1)),
        seq=11,
    )
    got = await _get_sla(client, tok, conv["id"])
    armed_due = got["next_response"]["due_at"]
    assert armed_due is not None

    # A SECOND contact follow-up must NOT push the deadline forward (regression guard).
    await _fold(
        ws,
        conv["id"],
        events.CONVERSATION_PART_CREATED,
        _part_payload(ws, conv["id"], author_kind="contact", at=now + dt.timedelta(minutes=5)),
        seq=12,
    )
    got = await _get_sla(client, tok, conv["id"])
    assert (
        got["next_response"]["due_at"] == armed_due
    )  # unchanged — anchored to the first follow-up

    # The next agent reply satisfies it.
    await _fold(
        ws,
        conv["id"],
        events.CONVERSATION_PART_CREATED,
        _part_payload(ws, conv["id"], author_kind="admin", at=now + dt.timedelta(minutes=6)),
        seq=13,
    )
    got = await _get_sla(client, tok, conv["id"])
    assert got["next_response"]["satisfied_at"] is not None


async def test_next_response_not_armed_before_first_response(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client, "SlaNextGuard")
    policy = await _policy(client, tok, first_response_seconds=3600, next_response_seconds=1800)
    conv = await _conversation(client, tok)
    await client.post(
        f"/v0/conversations/{conv['id']}/sla", json={"policy_id": policy["id"]}, headers=_auth(tok)
    )
    # A contact message while first-response is still pending must NOT arm next-response.
    await _fold(
        ws,
        conv["id"],
        events.CONVERSATION_PART_CREATED,
        _part_payload(ws, conv["id"], author_kind="contact", at=dt.datetime.now(_UTC)),
        seq=10,
    )
    got = await _get_sla(client, tok, conv["id"])
    assert got["next_response"]["due_at"] is None


# --- escalation: reassign + foreign-team rejection --------------------------------------------


async def test_breach_reassign_escalation(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client, "SlaReassign")
    team = await _team(client, tok)
    policy = await _policy(
        client, tok, first_response_seconds=3600, escalation={"reassign_team_id": team["id"]}
    )
    conv = await _conversation(client, tok)
    await client.post(
        f"/v0/conversations/{conv['id']}/sla", json={"policy_id": policy["id"]}, headers=_auth(tok)
    )
    await _force_due_in_past(ws, conv["id"])
    assert await sla.sweep_due_breaches() == 1

    convrow = (await client.get(f"/v0/conversations/{conv['id']}", headers=_auth(tok))).json()
    assert convrow["team_id"] == team["id"]  # routed to the escalation team
    assert convrow["assignee_id"] is None  # assignee cleared


async def test_policy_rejects_foreign_reassign_team(client: httpx.AsyncClient) -> None:
    tok_a, _ws_a = await _owner(client, "SlaEscA")
    tok_b, _ws_b = await _owner(client, "SlaEscB")
    team_b = await _team(client, tok_b)
    # Workspace A cannot set an escalation targeting workspace B's team.
    r = await client.post(
        "/v0/sla-policies",
        json={
            "name": "x",
            "first_response_seconds": 60,
            "escalation": {"reassign_team_id": team_b["id"]},
        },
        headers=_auth(tok_a),
    )
    assert r.status_code == 422, r.text


async def test_sweep_does_not_breach_before_due(client: httpx.AsyncClient) -> None:
    tok, _ws = await _owner(client, "SlaNotYet")
    policy = await _policy(client, tok, first_response_seconds=3600)
    conv = await _conversation(client, tok)
    await client.post(
        f"/v0/conversations/{conv['id']}/sla", json={"policy_id": policy["id"]}, headers=_auth(tok)
    )
    # Due is ~1h out; the sweep must not fire early.
    assert await sla.sweep_due_breaches() == 0
    got = await _get_sla(client, tok, conv["id"])
    assert got["first_response"]["breached_at"] is None
