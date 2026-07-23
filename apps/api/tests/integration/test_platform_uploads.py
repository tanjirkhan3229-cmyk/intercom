"""Presigned attachment upload/download (P0.5 composer attachments, RFC-001 A2 / §10).

Presigning is a local signing operation, so these run without a live MinIO. The security-relevant
behaviour is the workspace-prefix check on download: S3 has no RLS, so the API must refuse to sign
a GET for an object outside the caller's workspace.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

pytestmark = pytest.mark.integration

PASSWORD = "password123"


async def _owner(client: httpx.AsyncClient, ws_name: str) -> str:
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
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_presign_upload_returns_workspace_scoped_key(client: httpx.AsyncClient) -> None:
    tok = await _owner(client, "Uploads")
    r = await client.post(
        "/v0/uploads/presign",
        json={"filename": "screen shot.png", "content_type": "image/png"},
        headers=_auth(tok),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["key"].startswith("attachments/wrk_")
    assert body["key"].endswith("/screen_shot.png")  # filename sanitised
    assert body["upload_url"].startswith("http")
    assert body["method"] == "PUT"


async def test_download_url_rejects_foreign_workspace_key(client: httpx.AsyncClient) -> None:
    tok_a = await _owner(client, "DownloadA")
    tok_b = await _owner(client, "DownloadB")

    presigned = (
        await client.post(
            "/v0/uploads/presign",
            json={"filename": "a.pdf", "content_type": "application/pdf"},
            headers=_auth(tok_a),
        )
    ).json()
    key = presigned["key"]

    # Owner A can mint a download URL for their own object.
    ok = await client.get("/v0/uploads/download-url", params={"key": key}, headers=_auth(tok_a))
    assert ok.status_code == 200, ok.text
    assert ok.json()["url"].startswith("http")

    # Owner B cannot — the key is outside B's workspace prefix.
    denied = await client.get(
        "/v0/uploads/download-url", params={"key": key}, headers=_auth(tok_b)
    )
    assert denied.status_code == 403, denied.text
