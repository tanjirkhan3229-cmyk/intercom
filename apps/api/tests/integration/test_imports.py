"""CSV contact import/export integration tests (P1.9, RFC-002 §5.4).

The bulk import runs the real sync COPY-staged worker against testcontainers Postgres; S3 byte I/O
is faked in-memory (the logic under test is parse → dedupe → upsert → progress → error report, not
boto3). Covers: round-trip, idempotent re-run (zero duplicates — the acceptance), dedupe by BOTH
partial-unique keys, malformed rows don't fail the job, cross-tenant isolation, and export.
"""

from __future__ import annotations

import io
from uuid import uuid4

import httpx
import pytest

from relay.core import storage
from relay.core.ids import IdPrefix, decode_public_id
from relay.modules.crm import import_worker
from relay.modules.crm import service as crm_service
from relay.settings import get_settings

pytestmark = pytest.mark.integration

PASSWORD = "password123"


@pytest.fixture
def fake_s3(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], bytes]:
    """In-memory object store patched over storage's worker-side byte I/O."""
    store: dict[tuple[str, str], bytes] = {}

    def _open_read_stream(bucket: str, key: str) -> io.BytesIO:
        return io.BytesIO(store[(bucket, key)])

    def _upload_fileobj(bucket: str, key: str, fileobj: object, *, content_type: str = "") -> None:
        store[(bucket, key)] = fileobj.read()  # type: ignore[attr-defined]

    def _get_object(bucket: str, key: str) -> bytes:
        return store[(bucket, key)]

    monkeypatch.setattr(storage, "open_read_stream", _open_read_stream)
    monkeypatch.setattr(storage, "upload_fileobj", _upload_fileobj)
    monkeypatch.setattr(storage, "get_object", _get_object)
    return store


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


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _define_attr(client: httpx.AsyncClient, tok: str, name: str, data_type: str) -> None:
    resp = await client.post(
        "/v0/attribute-definitions",
        json={"entity": "contact", "name": name, "data_type": data_type},
        headers=_auth(tok),
    )
    assert resp.status_code == 201, resp.text


async def _upload_csv(
    client: httpx.AsyncClient, tok: str, store: dict[tuple[str, str], bytes], text: str
) -> str:
    """Presign, then stash the CSV bytes in the fake store under the presigned key."""
    presign = await client.post(
        "/v0/imports/contacts/presign",
        json={"filename": "contacts.csv", "content_type": "text/csv"},
        headers=_auth(tok),
    )
    assert presign.status_code == 200, presign.text
    key = presign.json()["key"]
    store[(get_settings().s3_bucket_attachments, key)] = text.encode("utf-8")
    return key


async def _run_import(
    client: httpx.AsyncClient, tok: str, ws_pub: str, key: str, mapping: dict
) -> str:
    created = await client.post(
        "/v0/imports/contacts",
        json={"s3_key": key, "filename": "contacts.csv", "column_mapping": mapping},
        headers=_auth(tok),
    )
    assert created.status_code == 201, created.text
    job_pub = created.json()["id"]
    ws = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    job_id = decode_public_id(IdPrefix.IMPORT_JOB, job_pub)
    result = import_worker.run_contact_import(str(job_id), str(ws), ws_pub)
    assert result == "completed", result
    return job_pub


async def _contact_count(client: httpx.AsyncClient, tok: str) -> int:
    resp = await client.get("/v0/contacts", params={"limit": 200}, headers=_auth(tok))
    return len(resp.json()["items"])


async def test_import_round_trip_and_idempotent(
    client: httpx.AsyncClient, fake_s3: dict[tuple[str, str], bytes]
) -> None:
    tok, ws_pub = await _owner(client, "ImportRT")
    await _define_attr(client, tok, "tier", "string")
    csv_text = "ext,email,tier\nu1,a@ex.com,pro\nu2,b@ex.com,free\nu3,c@ex.com,pro\n"
    mapping = {"ext": "external_id", "email": "email", "tier": "custom.tier"}

    key = await _upload_csv(client, tok, fake_s3, csv_text)
    job_pub = await _run_import(client, tok, ws_pub, key, mapping)

    got = await client.get(f"/v0/imports/{job_pub}", headers=_auth(tok))
    body = got.json()
    assert body["status"] == "completed"
    assert body["inserted_rows"] == 3
    assert body["updated_rows"] == 0
    assert body["error_rows"] == 0
    assert await _contact_count(client, tok) == 3

    # Idempotent re-run over the SAME file (a fresh job) → zero new contacts (the acceptance).
    key2 = await _upload_csv(client, tok, fake_s3, csv_text)
    job2 = await _run_import(client, tok, ws_pub, key2, mapping)
    body2 = (await client.get(f"/v0/imports/{job2}", headers=_auth(tok))).json()
    assert body2["inserted_rows"] == 0
    assert body2["updated_rows"] == 3
    assert await _contact_count(client, tok) == 3


async def test_dedupe_within_file_by_external_id_and_email(
    client: httpx.AsyncClient, fake_s3: dict[tuple[str, str], bytes]
) -> None:
    tok, ws_pub = await _owner(client, "ImportDedup")
    # Same external_id twice + same email twice (no external_id) — each collapses to one contact.
    csv_text = (
        "ext,email\n"
        "dup,first@ex.com\n"
        "dup,second@ex.com\n"  # same external_id → one contact (Pass A DISTINCT ON)
        ",same@ex.com\n"
        ",same@ex.com\n"  # same email, no external_id → one contact (Pass B DISTINCT ON)
    )
    mapping = {"ext": "external_id", "email": "email"}
    key = await _upload_csv(client, tok, fake_s3, csv_text)
    job_pub = await _run_import(client, tok, ws_pub, key, mapping)
    body = (await client.get(f"/v0/imports/{job_pub}", headers=_auth(tok))).json()
    assert body["inserted_rows"] == 2  # one per identity key
    assert await _contact_count(client, tok) == 2


async def test_shared_email_across_external_ids_does_not_abort_chunk(
    client: httpx.AsyncClient, fake_s3: dict[tuple[str, str], bytes]
) -> None:
    """Two rows with DIFFERENT external_id but the SAME email must not violate contacts_email_user
    and abort the chunk (the email index is not Pass A's conflict arbiter). Both contacts land; the
    email is assigned to exactly one of them (the other is left for the merge job)."""
    tok, ws_pub = await _owner(client, "ImportSharedEmail")
    csv_text = "ext,email\np,shared@ex.com\nq,shared@ex.com\n"
    mapping = {"ext": "external_id", "email": "email"}
    key = await _upload_csv(client, tok, fake_s3, csv_text)
    job_pub = await _run_import(client, tok, ws_pub, key, mapping)
    body = (await client.get(f"/v0/imports/{job_pub}", headers=_auth(tok))).json()
    assert body["status"] == "completed"
    assert body["inserted_rows"] == 2  # both external_id contacts created, no abort
    assert await _contact_count(client, tok) == 2
    # Exactly one of them ended up with the email (the unique index holds).
    items = (await client.get("/v0/contacts", params={"limit": 200}, headers=_auth(tok))).json()[
        "items"
    ]
    assert sorted(c["external_id"] for c in items) == ["p", "q"]
    assert [c["email"] for c in items].count("shared@ex.com") == 1


async def test_malformed_rows_do_not_fail_the_job(
    client: httpx.AsyncClient, fake_s3: dict[tuple[str, str], bytes]
) -> None:
    tok, ws_pub = await _owner(client, "ImportBad")
    csv_text = (
        "ext,email\n"
        "good1,ok@ex.com\n"
        ",not-an-email\n"  # invalid email + no external_id → error
        ",\n"  # missing identity → error
        "good2,ok2@ex.com\n"
    )
    mapping = {"ext": "external_id", "email": "email"}
    key = await _upload_csv(client, tok, fake_s3, csv_text)
    job_pub = await _run_import(client, tok, ws_pub, key, mapping)
    body = (await client.get(f"/v0/imports/{job_pub}", headers=_auth(tok))).json()
    assert body["status"] == "completed"
    assert body["inserted_rows"] == 2
    assert body["error_rows"] == 2
    assert body["error_report_url"] is not None
    assert await _contact_count(client, tok) == 2


async def test_import_cross_tenant_key_rejected(
    client: httpx.AsyncClient, fake_s3: dict[tuple[str, str], bytes]
) -> None:
    tok_a, ws_a = await _owner(client, "ImpA")
    tok_b, _ws_b = await _owner(client, "ImpB")
    key_a = await _upload_csv(client, tok_a, fake_s3, "ext,email\nu1,a@ex.com\n")

    # B cannot create an import over a key in A's workspace prefix.
    denied = await client.post(
        "/v0/imports/contacts",
        json={"s3_key": key_a, "filename": "x.csv", "column_mapping": {"ext": "external_id"}},
        headers=_auth(tok_b),
    )
    assert denied.status_code == 422, denied.text

    # And B cannot read A's job.
    job_a = await _run_import(client, tok_a, ws_a, key_a, {"ext": "external_id", "email": "email"})
    assert (await client.get(f"/v0/imports/{job_a}", headers=_auth(tok_b))).status_code == 404


async def test_export_round_trip(
    client: httpx.AsyncClient, fake_s3: dict[tuple[str, str], bytes]
) -> None:
    tok, ws_pub = await _owner(client, "ExportRT")
    for i in range(5):
        await client.post(
            "/v0/contacts/identify", json={"external_id": f"e{i}"}, headers=_auth(tok)
        )

    created = await client.post("/v0/exports/contacts", json={"filters": {}}, headers=_auth(tok))
    assert created.status_code == 201, created.text
    job_pub = created.json()["id"]
    ws = decode_public_id(IdPrefix.WORKSPACE, ws_pub)
    job_id = decode_public_id(IdPrefix.EXPORT_JOB, job_pub)
    result = await crm_service.run_contact_export(job_id, ws, ws_pub)
    assert result == "completed", result

    body = (await client.get(f"/v0/exports/{job_pub}", headers=_auth(tok))).json()
    assert body["status"] == "completed"
    assert body["row_count"] == 5
    assert body["download_url"] is not None

    # The generated CSV has a header + 5 data rows.
    key = f"{storage.export_prefix(ws_pub)}{job_id}/contacts.csv"
    data = fake_s3[(get_settings().s3_bucket_exports, key)].decode("utf-8").strip().splitlines()
    assert data[0].startswith("external_id,email,")
    assert len(data) == 6
