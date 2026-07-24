"""Workflow engine integration tests (P1.5 acceptance, RFC-001 §6.7, RFC-002 §5.6).

Drives the real trigger consumer + executor + tasks against a testcontainers Postgres/Redis (no
running worker/relay — tasks are invoked directly, like the webhook-delivery tests). Covers:
CRUD + publish + graph validation; trigger→run→execute; condition branches; every internal action;
durable waits (timer claim + fire + resume); bot steps + submit_input; contact-attribute writes;
the external call_webhook action; the execution log; and cross-tenant RLS isolation.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from relay.core.db import session_scope
from relay.core.ids import IdPrefix, decode_public_id, encode_public_id, uuid7
from relay.core.outbox import OutboxMessage
from relay.core.redis import get_redis
from relay.modules.automation import consumer, tasks
from relay.modules.automation.models import Timer, WorkflowRun, WorkflowRunStep
from relay.modules.messaging.models import ConversationPart, ConversationTag

pytestmark = pytest.mark.integration

PASSWORD = "password123"


# --- harness helpers ----------------------------------------------------------


async def _owner(client: httpx.AsyncClient, ws_name: str = "WF") -> tuple[str, str]:
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


async def _contact(client: httpx.AsyncClient, tok: str) -> str:
    r = await client.post(
        "/v0/contacts/identify", json={"external_id": uuid4().hex}, headers=_auth(tok)
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _conversation(client: httpx.AsyncClient, tok: str, *, body: str = "hi") -> dict:
    contact_id = await _contact(client, tok)
    r = await client.post(
        "/v0/conversations", json={"contact_id": contact_id, "body": body}, headers=_auth(tok)
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _publish(client: httpx.AsyncClient, tok: str, graph: dict, name: str = "wf") -> str:
    wf = (await client.post("/v0/workflows", json={"name": name}, headers=_auth(tok))).json()
    v = await client.post(
        f"/v0/workflows/{wf['id']}/versions", json={"graph": graph}, headers=_auth(tok)
    )
    assert v.status_code == 201, v.text
    pub = await client.post(
        f"/v0/workflows/{wf['id']}/publish", json={"version_id": v.json()["id"]}, headers=_auth(tok)
    )
    assert pub.status_code == 200, pub.text
    return wf["id"]


def _conv_payload(ws_pub: str, conv: dict, **extra: Any) -> dict[str, Any]:
    """A conversation-event payload matching messaging's ``_conversation_payload`` shape."""
    return {
        "workspace_id": ws_pub,
        "conversation_id": conv["id"],
        "contact_id": conv["contact_id"],
        "state": conv["state"],
        **extra,
    }


async def _fire(ws_pub: str, trigger_key: str, topic: str, payload: dict[str, Any]) -> list[str]:
    """Run the real consumer run-creation + advance each run (as the worker would). Returns run
    public ids."""
    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    run_ids = await consumer._create_runs(ws_uuid, trigger_key, topic, uuid7(), payload)
    for rid in run_ids:
        await tasks._advance_run(ws_uuid, rid)
    return [encode_public_id(IdPrefix.WORKFLOW_RUN, rid) for rid in run_ids]


async def _tag_names(ws_pub: str, conv_id: str) -> set[str]:
    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    cid = decode_public_id(IdPrefix.CONVERSATION, conv_id)
    async with session_scope(ws_uuid) as s:
        return set(
            (
                await s.scalars(
                    select(ConversationTag.name).where(ConversationTag.conversation_id == cid)
                )
            ).all()
        )


async def _run(client: httpx.AsyncClient, tok: str, run_pub: str) -> dict:
    r = await client.get(f"/v0/workflow_runs/{run_pub}", headers=_auth(tok))
    assert r.status_code == 200, r.text
    return r.json()


# --- graphs -------------------------------------------------------------------


def _add_tag_graph(name: str = "vip", trigger: str = "conversation.created") -> dict:
    return {
        "nodes": [
            {"id": "t", "type": "trigger", "trigger": trigger, "next": "a"},
            {
                "id": "a",
                "type": "action",
                "action": "add_tag",
                "params": {"name": name},
                "next": "e",
            },
            {"id": "e", "type": "end"},
        ]
    }


# --- CRUD + publish -----------------------------------------------------------


async def test_create_version_publish(client: httpx.AsyncClient) -> None:
    tok, _ws = await _owner(client)
    wf = (await client.post("/v0/workflows", json={"name": "greeter"}, headers=_auth(tok))).json()
    assert wf["status"] == "inactive" and wf["active_version_id"] is None
    v = await client.post(
        f"/v0/workflows/{wf['id']}/versions", json={"graph": _add_tag_graph()}, headers=_auth(tok)
    )
    assert v.status_code == 201, v.text
    assert v.json()["version"] == 1 and v.json()["trigger_key"] == "conversation.created"
    pub = await client.post(
        f"/v0/workflows/{wf['id']}/publish", json={"version_id": v.json()["id"]}, headers=_auth(tok)
    )
    assert pub.status_code == 200
    assert pub.json()["status"] == "active"
    assert pub.json()["active_version_id"] == v.json()["id"]


async def test_invalid_graph_rejected(client: httpx.AsyncClient) -> None:
    tok, _ws = await _owner(client)
    wf = (await client.post("/v0/workflows", json={"name": "bad"}, headers=_auth(tok))).json()
    bad = {
        "nodes": [
            {"id": "t", "type": "trigger", "trigger": "conversation.created", "next": "ghost"}
        ]
    }
    r = await client.post(
        f"/v0/workflows/{wf['id']}/versions", json={"graph": bad}, headers=_auth(tok)
    )
    assert r.status_code == 422
    assert "path" in r.json()["error"]["details"]


# --- trigger → run → execute (end-to-end via the real emitted outbox event) ---


async def test_trigger_creates_and_completes_run(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client)
    await _publish(client, tok, _add_tag_graph("vip"))
    conv = await _conversation(client, tok)

    # Use the REAL conversation.created event messaging wrote to the outbox (proves the wiring).
    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
    async with session_scope(ws_uuid) as s:
        row = (
            (
                await s.execute(
                    select(OutboxMessage)
                    .where(
                        OutboxMessage.topic == "conversation.created",
                        OutboxMessage.payload["conversation_id"].astext == conv["id"],
                    )
                    .order_by(OutboxMessage.seq.desc())
                )
            )
            .scalars()
            .first()
        )
    assert row is not None, "messaging did not emit conversation.created"

    run_ids = await consumer._create_runs(
        ws_uuid, "conversation.created", "conversation.created", row.id, row.payload
    )
    assert len(run_ids) == 1
    await tasks._advance_run(ws_uuid, run_ids[0])

    assert "vip" in await _tag_names(ws, conv["id"])
    run = await _run(client, tok, encode_public_id(IdPrefix.WORKFLOW_RUN, run_ids[0]))
    assert run["status"] == "completed"


async def test_trigger_filter_gates_run(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client)
    graph = _add_tag_graph("vip")
    graph["nodes"][0]["filter"] = {"op": "eq", "field": "state", "value": "snoozed"}
    await _publish(client, tok, graph)
    conv = await _conversation(client, tok)  # state=open → filter (wants snoozed) fails
    runs = await _fire(ws, "conversation.created", "conversation.created", _conv_payload(ws, conv))
    assert runs == []  # no workflow matched the filter → no run created


async def test_condition_branches(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client)
    graph = {
        "nodes": [
            {"id": "t", "type": "trigger", "trigger": "conversation.created", "next": "c"},
            {
                "id": "c",
                "type": "condition",
                "predicate": {"op": "eq", "field": "state", "value": "open"},
                "true": "yes",
                "false": "no",
            },
            {
                "id": "yes",
                "type": "action",
                "action": "add_tag",
                "params": {"name": "open_tag"},
                "next": "e",
            },
            {
                "id": "no",
                "type": "action",
                "action": "add_tag",
                "params": {"name": "other_tag"},
                "next": "e",
            },
            {"id": "e", "type": "end"},
        ]
    }
    await _publish(client, tok, graph)
    conv = await _conversation(client, tok)
    await _fire(ws, "conversation.created", "conversation.created", _conv_payload(ws, conv))
    tags = await _tag_names(ws, conv["id"])
    assert "open_tag" in tags and "other_tag" not in tags


# --- durable waits (timers) ---------------------------------------------------


async def test_wait_timer_resume(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client)
    graph = {
        "nodes": [
            {"id": "t", "type": "trigger", "trigger": "conversation.created", "next": "a1"},
            {
                "id": "a1",
                "type": "action",
                "action": "add_tag",
                "params": {"name": "first"},
                "next": "w",
            },
            {"id": "w", "type": "wait", "params": {"seconds": 3600}, "next": "a2"},
            {
                "id": "a2",
                "type": "action",
                "action": "add_tag",
                "params": {"name": "second"},
                "next": "e",
            },
            {"id": "e", "type": "end"},
        ]
    }
    await _publish(client, tok, graph)
    conv = await _conversation(client, tok)
    runs = await _fire(ws, "conversation.created", "conversation.created", _conv_payload(ws, conv))
    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
    rid = decode_public_id(IdPrefix.WORKFLOW_RUN, runs[0])

    # Parked on the wait: first tag applied, second not; a pending timer exists.
    assert await _tag_names(ws, conv["id"]) == {"first"}
    run = await _run(client, tok, runs[0])
    assert run["status"] == "waiting"
    async with session_scope(ws_uuid) as s:
        timer = (await s.execute(select(Timer).where(Timer.run_id == rid))).scalar_one()
        timer.fire_at = timer.created_at  # make it due now (was now+1h)

    # The beat claim (FOR UPDATE SKIP LOCKED) finds it; then fire + advance resumes the run.
    claimed = await tasks._scan_due_timers()
    assert claimed >= 1
    await tasks._fire_timer(ws_uuid, timer.id, rid)
    await tasks._advance_run(ws_uuid, rid)

    assert await _tag_names(ws, conv["id"]) == {"first", "second"}
    assert (await _run(client, tok, runs[0]))["status"] == "completed"


# --- bot steps ----------------------------------------------------------------


async def test_bot_step_and_submit_input(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client)
    graph = {
        "nodes": [
            {"id": "t", "type": "trigger", "trigger": "conversation.created", "next": "b"},
            {
                "id": "b",
                "type": "bot_step",
                "bot": "ask_buttons",
                "params": {
                    "prompt": "Pick one",
                    "options": [
                        {"label": "Yes", "value": "yes", "next": "ay"},
                        {"label": "No", "value": "no", "next": "an"},
                    ],
                },
            },
            {
                "id": "ay",
                "type": "action",
                "action": "add_tag",
                "params": {"name": "said_yes"},
                "next": "e",
            },
            {
                "id": "an",
                "type": "action",
                "action": "add_tag",
                "params": {"name": "said_no"},
                "next": "e",
            },
            {"id": "e", "type": "end"},
        ]
    }
    await _publish(client, tok, graph)
    conv = await _conversation(client, tok)
    runs = await _fire(ws, "conversation.created", "conversation.created", _conv_payload(ws, conv))
    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
    rid = decode_public_id(IdPrefix.WORKFLOW_RUN, runs[0])

    # Parked awaiting input; the bot prompt was posted as an ai_agent comment with workflow meta.
    assert (await _run(client, tok, runs[0]))["status"] == "awaiting_input"
    cid = decode_public_id(IdPrefix.CONVERSATION, conv["id"])
    async with session_scope(ws_uuid) as s:
        parts = (
            (
                await s.execute(
                    select(ConversationPart).where(
                        ConversationPart.conversation_id == cid,
                        ConversationPart.author_kind == "ai_agent",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(parts) == 1 and parts[0].meta.get("workflow", {}).get("node_id") == "b"

    # Submit the answer via the API, then advance (the worker would pick up the enqueued advance).
    r = await client.post(
        f"/v0/workflow_runs/{runs[0]}/input",
        json={"node_id": "b", "value": "yes"},
        headers=_auth(tok),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "running"
    await tasks._advance_run(ws_uuid, rid)

    assert "said_yes" in await _tag_names(ws, conv["id"])
    assert (await _run(client, tok, runs[0]))["status"] == "completed"


# --- contact-attribute write --------------------------------------------------


async def test_set_contact_attribute(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client)
    # Define the typed attribute first (crm swamp guard); an undefined key would 422 → step skipped.
    defn = await client.post(
        "/v0/attribute-definitions",
        json={"entity": "contact", "name": "tier", "data_type": "string"},
        headers=_auth(tok),
    )
    assert defn.status_code == 201, defn.text
    graph = {
        "nodes": [
            {"id": "t", "type": "trigger", "trigger": "conversation.created", "next": "a"},
            {
                "id": "a",
                "type": "action",
                "action": "set_attribute",
                "params": {"target": "contact", "key": "tier", "value": "gold"},
                "next": "e",
            },
            {"id": "e", "type": "end"},
        ]
    }
    await _publish(client, tok, graph)
    conv = await _conversation(client, tok)
    await _fire(ws, "conversation.created", "conversation.created", _conv_payload(ws, conv))

    got = await client.get(f"/v0/contacts/{conv['contact_id']}", headers=_auth(tok))
    assert got.json()["custom"].get("tier") == "gold"


# --- external call_webhook ----------------------------------------------------


class _Receiver:
    def __init__(self, status: int = 200) -> None:
        self.received: list[bytes] = []
        self.status = status  # mutable so a test can flip 500 → 200 between attempts
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_POST(self) -> None:
                n = int(self.headers.get("Content-Length", "0"))
                outer.received.append(self.rfile.read(n))
                self.send_response(outer.status)
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
def _allow_private() -> Iterator[None]:
    from relay.settings import get_settings

    old = os.environ.get("WEBHOOK_ALLOW_PRIVATE_TARGETS")
    os.environ["WEBHOOK_ALLOW_PRIVATE_TARGETS"] = "true"
    get_settings.cache_clear()
    yield
    if old is None:
        os.environ.pop("WEBHOOK_ALLOW_PRIVATE_TARGETS", None)
    else:
        os.environ["WEBHOOK_ALLOW_PRIVATE_TARGETS"] = old
    get_settings.cache_clear()


async def test_call_webhook_action(client: httpx.AsyncClient, _allow_private: None) -> None:
    tok, ws = await _owner(client)
    with _Receiver(status=200) as server:
        graph = {
            "nodes": [
                {"id": "t", "type": "trigger", "trigger": "conversation.created", "next": "a"},
                {
                    "id": "a",
                    "type": "action",
                    "action": "call_webhook",
                    "params": {"url": server.url},
                    "next": "e",
                },
                {"id": "e", "type": "end"},
            ]
        }
        await _publish(client, tok, graph)
        conv = await _conversation(client, tok)
        runs = await _fire(
            ws, "conversation.created", "conversation.created", _conv_payload(ws, conv)
        )
        ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
        rid = decode_public_id(IdPrefix.WORKFLOW_RUN, runs[0])

        # advance suspended the run on the external action; run the action task (sync HTTP) in a
        # thread (its own event loop), then advance to completion.
        assert (await _run(client, tok, runs[0]))["status"] == "suspended"
        result = await tasks._run_action(ws_uuid, rid, "a")
        assert result == "done"
        await tasks._advance_run(ws_uuid, rid)

        assert len(server.received) == 1
        assert (await _run(client, tok, runs[0]))["status"] == "completed"


# --- execution log ------------------------------------------------------------


async def test_execution_log(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client)
    await _publish(client, tok, _add_tag_graph("logged"))
    conv = await _conversation(client, tok)
    runs = await _fire(ws, "conversation.created", "conversation.created", _conv_payload(ws, conv))
    steps = await client.get(f"/v0/workflow_runs/{runs[0]}/steps", headers=_auth(tok))
    assert steps.status_code == 200
    by_node = {s["node_id"]: s for s in steps.json()}
    assert by_node["a"]["status"] == "done" and by_node["a"]["action_type"] == "add_tag"


# --- cross-tenant isolation ---------------------------------------------------


async def test_cross_tenant_isolation(client: httpx.AsyncClient) -> None:
    tok_a, _ws_a = await _owner(client, "Alpha")
    wf_a = (
        await client.post("/v0/workflows", json={"name": "secret"}, headers=_auth(tok_a))
    ).json()

    tok_b, _ws_b = await _owner(client, "Bravo")
    # B cannot read A's workflow, and B's list is empty (RLS scopes every query).
    assert (
        await client.get(f"/v0/workflows/{wf_a['id']}", headers=_auth(tok_b))
    ).status_code == 404
    listing = await client.get("/v0/workflows", headers=_auth(tok_b))
    assert listing.json()["items"] == []


async def test_cross_tenant_runs_invisible(client: httpx.AsyncClient) -> None:
    """A run/step created in workspace A is invisible under workspace B's RLS GUC (table-level)."""
    tok_a, ws_a = await _owner(client, "AlphaRuns")
    await _publish(client, tok_a, _add_tag_graph("x"))
    conv = await _conversation(client, tok_a)
    await _fire(ws_a, "conversation.created", "conversation.created", _conv_payload(ws_a, conv))
    _tok_b, ws_b = await _owner(client, "BravoRuns")
    b_uuid = decode_public_id(IdPrefix.WORKSPACE, ws_b)
    async with session_scope(b_uuid) as s:  # under B's GUC, A's runs/steps are not visible
        assert (await s.scalars(select(WorkflowRun))).all() == []
        assert (await s.scalars(select(WorkflowRunStep))).all() == []


# --- version pinning (in-flight runs keep their version) ----------------------


async def test_version_pinning(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client)
    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)

    def _versioned_graph(after_tag: str) -> dict:
        return {
            "nodes": [
                {"id": "t", "type": "trigger", "trigger": "conversation.created", "next": "a1"},
                {
                    "id": "a1",
                    "type": "action",
                    "action": "add_tag",
                    "params": {"name": "before"},
                    "next": "w",
                },
                {"id": "w", "type": "wait", "params": {"seconds": 3600}, "next": "a2"},
                {
                    "id": "a2",
                    "type": "action",
                    "action": "add_tag",
                    "params": {"name": after_tag},
                    "next": "e",
                },
                {"id": "e", "type": "end"},
            ]
        }

    wf = (await client.post("/v0/workflows", json={"name": "pinned"}, headers=_auth(tok))).json()
    v1 = await client.post(
        f"/v0/workflows/{wf['id']}/versions",
        json={"graph": _versioned_graph("v1_after")},
        headers=_auth(tok),
    )
    v1_id = v1.json()["id"]
    await client.post(
        f"/v0/workflows/{wf['id']}/publish", json={"version_id": v1_id}, headers=_auth(tok)
    )

    conv = await _conversation(client, tok)
    runs = await _fire(ws, "conversation.created", "conversation.created", _conv_payload(ws, conv))
    rid = decode_public_id(IdPrefix.WORKFLOW_RUN, runs[0])
    assert (await _run(client, tok, runs[0]))["status"] == "waiting"  # parked, pinned to v1

    # Publish v2 with a DIFFERENT post-wait tag while the run is in-flight.
    v2 = await client.post(
        f"/v0/workflows/{wf['id']}/versions",
        json={"graph": _versioned_graph("v2_after")},
        headers=_auth(tok),
    )
    v2_id = v2.json()["id"]
    await client.post(
        f"/v0/workflows/{wf['id']}/publish", json={"version_id": v2_id}, headers=_auth(tok)
    )

    # Resume the pinned run.
    async with session_scope(ws_uuid) as s:
        timer = (await s.execute(select(Timer).where(Timer.run_id == rid))).scalar_one()
        timer.fire_at = timer.created_at
    await tasks._fire_timer(ws_uuid, timer.id, rid)
    await tasks._advance_run(ws_uuid, rid)

    tags = await _tag_names(ws, conv["id"])
    assert "v1_after" in tags and "v2_after" not in tags  # ran v1's graph, not v2's
    run = await _run(client, tok, runs[0])
    assert run["status"] == "completed" and run["workflow_version_id"] == v1_id


# --- internal actions ---------------------------------------------------------


async def _me_admin_id(client: httpx.AsyncClient, tok: str) -> str:
    return (await client.get("/v0/auth/me", headers=_auth(tok))).json()["admin"]["id"]


async def _get_conv(client: httpx.AsyncClient, tok: str, conv_id: str) -> dict:
    return (await client.get(f"/v0/conversations/{conv_id}", headers=_auth(tok))).json()


def _one_action(action: str, params: dict) -> dict:
    return {
        "nodes": [
            {"id": "t", "type": "trigger", "trigger": "conversation.created", "next": "a"},
            {"id": "a", "type": "action", "action": action, "params": params, "next": "e"},
            {"id": "e", "type": "end"},
        ]
    }


async def test_action_close(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client)
    await _publish(client, tok, _one_action("close", {}))
    conv = await _conversation(client, tok)
    await _fire(ws, "conversation.created", "conversation.created", _conv_payload(ws, conv))
    assert (await _get_conv(client, tok, conv["id"]))["state"] == "closed"


async def test_action_snooze(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client)
    await _publish(client, tok, _one_action("snooze", {"seconds": 3600}))
    conv = await _conversation(client, tok)
    await _fire(ws, "conversation.created", "conversation.created", _conv_payload(ws, conv))
    got = await _get_conv(client, tok, conv["id"])
    assert got["state"] == "snoozed" and got["snoozed_until"] is not None


async def test_action_hand_to_aide(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client)
    await _publish(client, tok, _one_action("hand_to_aide", {}))
    conv = await _conversation(client, tok)
    await _fire(ws, "conversation.created", "conversation.created", _conv_payload(ws, conv))
    assert (await _get_conv(client, tok, conv["id"]))["ai_status"] == "active"


async def test_action_assign(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client)
    admin_id = await _me_admin_id(client, tok)
    await _publish(client, tok, _one_action("assign", {"assignee_id": admin_id}))
    conv = await _conversation(client, tok)
    await _fire(ws, "conversation.created", "conversation.created", _conv_payload(ws, conv))
    assert (await _get_conv(client, tok, conv["id"]))["assignee_id"] == admin_id


async def test_action_route_to_team(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client)
    team = (await client.post("/v0/teams", json={"name": "Support"}, headers=_auth(tok))).json()
    await _publish(client, tok, _one_action("route_to_team", {"team_id": team["id"]}))
    conv = await _conversation(client, tok)
    await _fire(ws, "conversation.created", "conversation.created", _conv_payload(ws, conv))
    got = await _get_conv(client, tok, conv["id"])
    assert got["team_id"] == team["id"] and got["assignee_id"] is None


async def test_action_set_conversation_attribute(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client)
    await _publish(
        client,
        tok,
        _one_action("set_attribute", {"target": "conversation", "key": "vip", "value": True}),
    )
    conv = await _conversation(client, tok)
    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
    cid = decode_public_id(IdPrefix.CONVERSATION, conv["id"])
    await _fire(ws, "conversation.created", "conversation.created", _conv_payload(ws, conv))
    from relay.modules.messaging.models import Conversation

    async with session_scope(ws_uuid) as s:
        row = (await s.execute(select(Conversation).where(Conversation.id == cid))).scalar_one()
        assert row.attributes.get("vip") is True


async def test_apply_sla_flag_off_is_skipped(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client)
    await _publish(client, tok, _one_action("apply_sla", {"policy": "gold"}))
    conv = await _conversation(client, tok)
    runs = await _fire(ws, "conversation.created", "conversation.created", _conv_payload(ws, conv))
    # apply_sla is registered but flag-gated off (P1.7): the step is skipped, the run completes.
    steps = (await client.get(f"/v0/workflow_runs/{runs[0]}/steps", headers=_auth(tok))).json()
    sla = next(s for s in steps if s["node_id"] == "a")
    assert sla["status"] == "skipped"
    assert (await _run(client, tok, runs[0]))["status"] == "completed"


# --- bot kinds: collect + disambiguate ----------------------------------------


async def test_bot_collect_stores_attribute(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client)
    await client.post(
        "/v0/attribute-definitions",
        json={"entity": "contact", "name": "reason", "data_type": "string"},
        headers=_auth(tok),
    )
    graph = {
        "nodes": [
            {"id": "t", "type": "trigger", "trigger": "conversation.created", "next": "b"},
            {
                "id": "b",
                "type": "bot_step",
                "bot": "collect",
                "params": {"prompt": "Why?", "target": "contact", "key": "reason", "next": "e"},
            },
            {"id": "e", "type": "end"},
        ]
    }
    await _publish(client, tok, graph)
    conv = await _conversation(client, tok)
    runs = await _fire(ws, "conversation.created", "conversation.created", _conv_payload(ws, conv))
    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
    rid = decode_public_id(IdPrefix.WORKFLOW_RUN, runs[0])
    assert (await _run(client, tok, runs[0]))["status"] == "awaiting_input"
    r = await client.post(
        f"/v0/workflow_runs/{runs[0]}/input",
        json={"node_id": "b", "value": "broken widget"},
        headers=_auth(tok),
    )
    assert r.status_code == 200
    await tasks._advance_run(ws_uuid, rid)
    got = await client.get(f"/v0/contacts/{conv['contact_id']}", headers=_auth(tok))
    assert got.json()["custom"].get("reason") == "broken widget"
    assert (await _run(client, tok, runs[0]))["status"] == "completed"


async def test_bot_disambiguate_default_next(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client)
    graph = {
        "nodes": [
            {"id": "t", "type": "trigger", "trigger": "conversation.created", "next": "b"},
            {
                "id": "b",
                "type": "bot_step",
                "bot": "disambiguate",
                "params": {
                    "prompt": "Which?",
                    "default_next": "fallback",
                    "options": [{"label": "Billing", "value": "billing", "next": "bill"}],
                },
            },
            {
                "id": "bill",
                "type": "action",
                "action": "add_tag",
                "params": {"name": "billing"},
                "next": "e",
            },
            {
                "id": "fallback",
                "type": "action",
                "action": "add_tag",
                "params": {"name": "fallback"},
                "next": "e",
            },
            {"id": "e", "type": "end"},
        ]
    }
    await _publish(client, tok, graph)
    conv = await _conversation(client, tok)
    runs = await _fire(ws, "conversation.created", "conversation.created", _conv_payload(ws, conv))
    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
    rid = decode_public_id(IdPrefix.WORKFLOW_RUN, runs[0])
    # A value matching no option falls through to default_next.
    r = await client.post(
        f"/v0/workflow_runs/{runs[0]}/input",
        json={"node_id": "b", "value": "unknown"},
        headers=_auth(tok),
    )
    assert r.status_code == 200
    await tasks._advance_run(ws_uuid, rid)
    assert "fallback" in await _tag_names(ws, conv["id"])


# --- triggers -----------------------------------------------------------------


async def test_contact_message_trigger_and_author_gating(client: httpx.AsyncClient) -> None:
    from relay.modules.automation import events as auto_events

    # Author gating (unit-level on the mapping): only a contact comment maps to the trigger.
    assert (
        auto_events.trigger_key_for(
            "conversation.part.created", {"part_type": "comment", "author_kind": "contact"}
        )
        == "contact.message.created"
    )
    assert (
        auto_events.trigger_key_for(
            "conversation.part.created", {"part_type": "comment", "author_kind": "admin"}
        )
        is None
    )

    # End-to-end: a contact message fires the trigger and the action runs.
    tok, ws = await _owner(client)
    await _publish(client, tok, _add_tag_graph("replied", trigger="contact.message.created"))
    conv = await _conversation(client, tok)
    part = _conv_payload(ws, conv, part_type="comment", author_kind="contact")
    runs = await _fire(ws, "contact.message.created", "conversation.part.created", part)
    assert len(runs) == 1
    assert "replied" in await _tag_names(ws, conv["id"])


async def test_condition_false_branch(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client)
    graph = {
        "nodes": [
            {"id": "t", "type": "trigger", "trigger": "conversation.created", "next": "c"},
            {
                "id": "c",
                "type": "condition",
                "predicate": {"op": "eq", "field": "state", "value": "closed"},  # conv is 'open'
                "true": "yes",
                "false": "no",
            },
            {
                "id": "yes",
                "type": "action",
                "action": "add_tag",
                "params": {"name": "was_true"},
                "next": "e",
            },
            {
                "id": "no",
                "type": "action",
                "action": "add_tag",
                "params": {"name": "was_false"},
                "next": "e",
            },
            {"id": "e", "type": "end"},
        ]
    }
    await _publish(client, tok, graph)
    conv = await _conversation(client, tok)
    await _fire(ws, "conversation.created", "conversation.created", _conv_payload(ws, conv))
    tags = await _tag_names(ws, conv["id"])
    assert "was_false" in tags and "was_true" not in tags


async def test_framework_trigger_publishes_but_never_fires(client: httpx.AsyncClient) -> None:
    # A framework-ready trigger (schedule) with no live source still publishes; it just never fires.
    tok, ws = await _owner(client)
    wf_id = await _publish(client, tok, _add_tag_graph("s", trigger="schedule"))
    assert wf_id
    conv = await _conversation(client, tok)
    runs = await _fire(ws, "conversation.created", "conversation.created", _conv_payload(ws, conv))
    assert runs == []  # a conversation event does not fire a schedule-triggered workflow


# --- guards + lifecycle -------------------------------------------------------


async def test_cancel_run(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client)
    graph = {
        "nodes": [
            {"id": "t", "type": "trigger", "trigger": "conversation.created", "next": "w"},
            {"id": "w", "type": "wait", "params": {"seconds": 3600}, "next": "e"},
            {"id": "e", "type": "end"},
        ]
    }
    await _publish(client, tok, graph)
    conv = await _conversation(client, tok)
    runs = await _fire(ws, "conversation.created", "conversation.created", _conv_payload(ws, conv))
    c = await client.post(f"/v0/workflow_runs/{runs[0]}/cancel", headers=_auth(tok))
    assert c.status_code == 200 and c.json()["status"] == "cancelled"
    again = await client.post(f"/v0/workflow_runs/{runs[0]}/cancel", headers=_auth(tok))
    assert again.status_code == 409  # already terminal


async def test_submit_input_guard_not_awaiting(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client)
    await _publish(client, tok, _add_tag_graph("x"))  # no bot step → run completes
    conv = await _conversation(client, tok)
    runs = await _fire(ws, "conversation.created", "conversation.created", _conv_payload(ws, conv))
    r = await client.post(
        f"/v0/workflow_runs/{runs[0]}/input",
        json={"node_id": "a", "value": "x"},
        headers=_auth(tok),
    )
    assert r.status_code == 409  # completed run is not awaiting input


# --- call_webhook failure paths -----------------------------------------------


async def test_call_webhook_transient_then_success(
    client: httpx.AsyncClient, _allow_private: None
) -> None:
    tok, ws = await _owner(client)
    with _Receiver(status=500) as server:
        await _publish(client, tok, _one_action("call_webhook", {"url": server.url}))
        conv = await _conversation(client, tok)
        runs = await _fire(
            ws, "conversation.created", "conversation.created", _conv_payload(ws, conv)
        )
        ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
        rid = decode_public_id(IdPrefix.WORKFLOW_RUN, runs[0])

        assert await tasks._run_action(ws_uuid, rid, "a") == "retry"  # 500 → transient
        assert (await _run(client, tok, runs[0]))["status"] == "suspended"

        async with session_scope(ws_uuid) as s:  # lapse the per-attempt lease
            step = (
                await s.execute(
                    select(WorkflowRunStep).where(
                        WorkflowRunStep.run_id == rid, WorkflowRunStep.node_id == "a"
                    )
                )
            ).scalar_one()
            step.updated_at = step.created_at - dt.timedelta(seconds=120)
        server.status = 200
        assert await tasks._run_action(ws_uuid, rid, "a") == "done"
        await tasks._advance_run(ws_uuid, rid)
        assert (await _run(client, tok, runs[0]))["status"] == "completed"


@pytest.fixture
def _no_retries() -> Iterator[None]:
    from relay.settings import get_settings

    keys = {"WEBHOOK_ALLOW_PRIVATE_TARGETS": "true", "WORKFLOW_ACTION_MAX_RETRIES": "0"}
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update(keys)
    get_settings.cache_clear()
    yield
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_settings.cache_clear()


async def test_call_webhook_permanent_failure_fails_run(
    client: httpx.AsyncClient, _no_retries: None
) -> None:
    tok, ws = await _owner(client)
    with _Receiver(status=500) as server:
        await _publish(client, tok, _one_action("call_webhook", {"url": server.url}))
        conv = await _conversation(client, tok)
        runs = await _fire(
            ws, "conversation.created", "conversation.created", _conv_payload(ws, conv)
        )
        ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
        rid = decode_public_id(IdPrefix.WORKFLOW_RUN, runs[0])
        # max_retries=0 → the first failed attempt is terminal; the executor fails the run.
        assert await tasks._run_action(ws_uuid, rid, "a") == "failed"
        await tasks._advance_run(ws_uuid, rid)
        assert (await _run(client, tok, runs[0]))["status"] == "failed"


async def test_suspended_run_reaper_recovers(
    client: httpx.AsyncClient, _allow_private: None
) -> None:
    tok, ws = await _owner(client)
    with _Receiver(status=200) as server:
        await _publish(client, tok, _one_action("call_webhook", {"url": server.url}))
        conv = await _conversation(client, tok)
        runs = await _fire(
            ws, "conversation.created", "conversation.created", _conv_payload(ws, conv)
        )
        ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
        rid = decode_public_id(IdPrefix.WORKFLOW_RUN, runs[0])
        assert (await _run(client, tok, runs[0]))["status"] == "suspended"
        async with session_scope(ws_uuid) as s:  # its run_action message was "lost"
            run = (
                await s.execute(select(WorkflowRun).where(WorkflowRun.id == rid).with_for_update())
            ).scalar_one()
            run.updated_at = run.created_at - dt.timedelta(hours=1)
        assert await tasks._scan_stuck_runs() >= 1  # reaper finds the suspended run
        assert await tasks._run_action(ws_uuid, rid, "a") == "done"
        await tasks._advance_run(ws_uuid, rid)
        assert (await _run(client, tok, runs[0]))["status"] == "completed"


# --- loop protection + poison-message robustness (consumer) -------------------


async def test_workflow_origin_event_is_not_retriggered(client: httpx.AsyncClient) -> None:
    """A domain event marked ``origin=workflow`` (emitted by a workflow action) must not create a
    new run — otherwise a ``contact.updated → set contact attribute`` workflow cascades forever."""
    tok, ws = await _owner(client)
    await client.post(
        "/v0/attribute-definitions",
        json={"entity": "contact", "name": "flag", "data_type": "boolean"},
        headers=_auth(tok),
    )
    graph = {
        "nodes": [
            {"id": "t", "type": "trigger", "trigger": "contact.updated", "next": "a"},
            {
                "id": "a",
                "type": "action",
                "action": "set_attribute",
                "params": {"target": "contact", "key": "flag", "value": True},
                "next": "e",
            },
            {"id": "e", "type": "end"},
        ]
    }
    await _publish(client, tok, graph)
    contact_id = await _contact(client, tok)
    redis = get_redis()
    await consumer.ensure_group(redis)

    def _fields(origin: bool) -> dict[str, str]:
        payload: dict[str, Any] = {"workspace_id": ws, "contact_id": contact_id}
        if origin:
            payload["origin"] = "workflow"
        return {
            "topic": "crm.contact.updated",
            "outbox_id": str(uuid7()),
            "payload": json.dumps(payload),
        }

    # A genuine contact.updated (no origin) creates a run…
    assert await consumer._handle_entry(redis, consumer.GROUP, "0-0", _fields(origin=False)) is True
    # …but a workflow-originated one is acked + skipped (loop protection).
    assert await consumer._handle_entry(redis, consumer.GROUP, "0-1", _fields(origin=True)) is False

    listing = await client.get("/v0/workflow_runs", headers=_auth(tok))
    assert len(listing.json()["items"]) == 1  # exactly one run — the cascade was blocked


async def test_malformed_payload_is_acked_not_retried(client: httpx.AsyncClient) -> None:
    """A payload with a malformed subject id is acked + skipped (returns False), never a poison
    message that wedges the stream in a retry hot-loop."""
    tok, ws = await _owner(client)
    await _publish(client, tok, _add_tag_graph("x"))
    redis = get_redis()
    await consumer.ensure_group(redis)
    fields = {
        "topic": "conversation.created",
        "outbox_id": str(uuid7()),
        # 'cnv_@@@' is not valid base62 → decode raises ValueError → ack + skip (not retry).
        "payload": json.dumps(
            {
                "workspace_id": ws,
                "conversation_id": "cnv_@@@",
                "contact_id": "usr_1",
                "state": "open",
            }
        ),
    }
    assert await consumer._handle_entry(redis, consumer.GROUP, "0-2", fields) is False


async def test_call_webhook_breaker_open_does_not_burn_attempt(
    client: httpx.AsyncClient, _allow_private: None
) -> None:
    """A per-host breaker open (tripped by other traffic) must not consume this run's retry budget:
    the re-drive issues no POST and the attempt count is rolled back."""
    from urllib.parse import urlsplit

    from relay.core.breaker import RedisCircuitBreaker
    from relay.core.redis import get_redis_sync
    from relay.settings import get_settings

    tok, ws = await _owner(client)
    with _Receiver(status=200) as server:
        await _publish(client, tok, _one_action("call_webhook", {"url": server.url}))
        conv = await _conversation(client, tok)
        runs = await _fire(
            ws, "conversation.created", "conversation.created", _conv_payload(ws, conv)
        )
        ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
        rid = decode_public_id(IdPrefix.WORKFLOW_RUN, runs[0])

        s = get_settings()
        host = urlsplit(server.url).hostname
        breaker = RedisCircuitBreaker(
            get_redis_sync(),
            f"wf-action:{host}",
            threshold=s.workflow_breaker_threshold,
            cooldown_seconds=s.workflow_breaker_cooldown_seconds,
        )
        for _ in range(s.workflow_breaker_threshold):
            breaker.record_failure()
        assert breaker.is_open()

        assert await tasks._run_action(ws_uuid, rid, "a") == "breaker_open"
        assert len(server.received) == 0  # no POST issued
        assert (await _run(client, tok, runs[0]))["status"] == "suspended"
        async with session_scope(ws_uuid) as sess:
            step = (
                await sess.execute(
                    select(WorkflowRunStep).where(
                        WorkflowRunStep.run_id == rid, WorkflowRunStep.node_id == "a"
                    )
                )
            ).scalar_one()
        assert step.attempt == 0 and step.status == "started"  # attempt not burned; still retryable


async def test_call_webhook_4xx_is_permanent_no_retry(
    client: httpx.AsyncClient, _allow_private: None
) -> None:
    """A 4xx client error fails the run immediately (one POST, no retries) and does NOT trip the
    per-host breaker — our request is bad, not the host."""
    from urllib.parse import urlsplit

    from relay.core.breaker import RedisCircuitBreaker
    from relay.core.redis import get_redis_sync
    from relay.settings import get_settings

    tok, ws = await _owner(client)
    with _Receiver(status=400) as server:
        await _publish(client, tok, _one_action("call_webhook", {"url": server.url}))
        conv = await _conversation(client, tok)
        runs = await _fire(
            ws, "conversation.created", "conversation.created", _conv_payload(ws, conv)
        )
        ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
        rid = decode_public_id(IdPrefix.WORKFLOW_RUN, runs[0])

        assert await tasks._run_action(ws_uuid, rid, "a") == "failed"  # permanent, not "retry"
        assert len(server.received) == 1  # exactly one POST, no retries
        await tasks._advance_run(ws_uuid, rid)
        assert (await _run(client, tok, runs[0]))["status"] == "failed"

        # The per-host breaker was not tripped by the 4xx.
        s = get_settings()
        host = urlsplit(server.url).hostname
        breaker = RedisCircuitBreaker(
            get_redis_sync(),
            f"wf-action:{host}",
            threshold=s.workflow_breaker_threshold,
            cooldown_seconds=s.workflow_breaker_cooldown_seconds,
        )
        assert not breaker.is_open()
