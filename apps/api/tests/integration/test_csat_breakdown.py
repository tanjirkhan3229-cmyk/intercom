"""Integration tests for the CSAT team/agent breakdown (P1.7 S6).

Seeds ``conversation_metrics`` (the reporting projection — never raw parts) against real
conversations, then reconciles the ``/reports/csat/breakdown`` averages by team and by agent against
hand-computed values, plus the date-window and unrated-exclusion rules.
"""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import httpx
import pytest

from relay.core.db import session_scope
from relay.core.ids import IdPrefix, decode_public_id, encode_public_id, uuid7
from relay.modules.reporting.models import ConversationMetric

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


async def _conversation_uuid(client: httpx.AsyncClient, tok: str) -> object:
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
    return decode_public_id(IdPrefix.CONVERSATION, conv["id"])


async def test_breakdown_reconciles_by_team_and_agent(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client, "Csat")
    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
    team1, team2 = uuid7(), uuid7()
    agent1, agent2 = uuid7(), uuid7()
    now = dt.datetime.now(_UTC)
    # (team, agent, rating): team1 → [5,3,4] (avg 4.0), team2 → [2]; agent1 → [5,3] (avg 4.0),
    # agent2 → [4,2] (avg 3.0). Plus one unrated row that must be ignored.
    seed = [
        (team1, agent1, 5),
        (team1, agent1, 3),
        (team1, agent2, 4),
        (team2, agent2, 2),
    ]
    async with session_scope(ws_uuid) as session:
        for team, agent, rating in seed:
            session.add(
                ConversationMetric(
                    workspace_id=ws_uuid,
                    conversation_id=await _conversation_uuid(client, tok),
                    team_id=team,
                    assignee_id=agent,
                    rating=rating,
                    rated_at=now,
                    opened_at=now,
                )
            )
        # An unrated conversation — excluded from CSAT.
        session.add(
            ConversationMetric(
                workspace_id=ws_uuid,
                conversation_id=await _conversation_uuid(client, tok),
                team_id=team1,
                assignee_id=agent1,
                rating=None,
                opened_at=now,
            )
        )

    body = (await client.get("/v0/reports/csat/breakdown", headers=_auth(tok))).json()
    by_team = {g["key"]: g for g in body["by_team"]}
    by_agent = {g["key"]: g for g in body["by_agent"]}

    t1, t2 = encode_public_id(IdPrefix.TEAM, team1), encode_public_id(IdPrefix.TEAM, team2)
    a1, a2 = encode_public_id(IdPrefix.ADMIN, agent1), encode_public_id(IdPrefix.ADMIN, agent2)

    assert by_team[t1] == {"key": t1, "count": 3, "average": 4.0}
    assert by_team[t2] == {"key": t2, "count": 1, "average": 2.0}
    assert by_agent[a1] == {"key": a1, "count": 2, "average": 4.0}
    assert by_agent[a2] == {"key": a2, "count": 2, "average": 3.0}


async def test_breakdown_respects_date_window(client: httpx.AsyncClient) -> None:
    tok, ws = await _owner(client, "CsatWindow")
    ws_uuid = decode_public_id(IdPrefix.WORKSPACE, ws)
    team = uuid7()
    old = dt.datetime.now(_UTC) - dt.timedelta(
        days=60
    )  # outside the default trailing-30-day window
    async with session_scope(ws_uuid) as session:
        session.add(
            ConversationMetric(
                workspace_id=ws_uuid,
                conversation_id=await _conversation_uuid(client, tok),
                team_id=team,
                assignee_id=uuid7(),
                rating=5,
                rated_at=old,
                opened_at=old,
            )
        )
    # Default window (trailing 30 days) excludes the 60-day-old rating.
    body = (await client.get("/v0/reports/csat/breakdown", headers=_auth(tok))).json()
    assert body["by_team"] == []
    assert body["by_agent"] == []

    # Widening the window to include it surfaces the rating.
    frm = (old - dt.timedelta(days=1)).date().isoformat()
    to = dt.datetime.now(_UTC).date().isoformat()
    body2 = (
        await client.get(f"/v0/reports/csat/breakdown?from={frm}&to={to}", headers=_auth(tok))
    ).json()
    by_team_count = {g["key"]: g["count"] for g in body2["by_team"]}
    assert by_team_count[encode_public_id(IdPrefix.TEAM, team)] == 1
