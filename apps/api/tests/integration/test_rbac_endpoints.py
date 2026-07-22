"""RBAC enforced at the service choke point, exercised over the HTTP surface."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from relay.core.ids import IdPrefix, decode_public_id
from relay.core.security import create_access_token

pytestmark = pytest.mark.integration

PASSWORD = "password123"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_agent_cannot_do_admin_actions_but_owner_can(client: httpx.AsyncClient) -> None:
    signup = await client.post(
        "/v0/auth/signup",
        json={
            "workspace_name": "RbacCo",
            "email": f"owner-{uuid4().hex}@example.com",
            "password": PASSWORD,
            "name": "Owner",
        },
    )
    owner = signup.json()
    owner_token = owner["access_token"]
    ws_id = decode_public_id(IdPrefix.WORKSPACE, owner["workspace"]["id"])

    # Owner invites an agent.
    invite = await client.post(
        "/v0/members",
        json={"email": f"agent-{uuid4().hex}@example.com", "name": "Agent", "role": "agent"},
        headers=_auth(owner_token),
    )
    assert invite.status_code == 201
    agent_admin_id = decode_public_id(IdPrefix.ADMIN, invite.json()["admin"]["id"])

    # Mint an access token for the agent (login needs a password; not required to test RBAC).
    agent_token = create_access_token(admin_id=agent_admin_id, workspace_id=ws_id, role="agent")

    # Agent may read teams...
    assert (await client.get("/v0/teams", headers=_auth(agent_token))).status_code == 200
    # ...but not create one (requires admin).
    forbidden = await client.post("/v0/teams", json={"name": "X"}, headers=_auth(agent_token))
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "permission_denied"

    # Owner can.
    assert (
        await client.post("/v0/teams", json={"name": "Y"}, headers=_auth(owner_token))
    ).status_code == 201


async def test_api_key_create_returns_key_once_then_hidden(client: httpx.AsyncClient) -> None:
    signup = await client.post(
        "/v0/auth/signup",
        json={
            "workspace_name": "KeyCo",
            "email": f"owner-{uuid4().hex}@example.com",
            "password": PASSWORD,
            "name": "Owner",
        },
    )
    token = signup.json()["access_token"]

    created = await client.post(
        "/v0/api-keys", json={"name": "CI", "scopes": ["contacts:read"]}, headers=_auth(token)
    )
    assert created.status_code == 201
    body = created.json()
    assert body["key"].startswith("relaysk_")  # full key returned exactly once
    assert body["id"].startswith("key_")

    listed = await client.get("/v0/api-keys", headers=_auth(token))
    assert listed.status_code == 200
    # The list never exposes the secret, only the prefix.
    assert "key" not in listed.json()[0]
    assert listed.json()[0]["key_prefix"].startswith("relaysk_")
