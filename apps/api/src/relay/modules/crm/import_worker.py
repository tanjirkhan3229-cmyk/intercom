"""CSV contact-import worker internals (P1.9, RFC-002 §5.4 / W2).

The 1M-row-in-<15-min path. Runs in a Celery task (sync ``psycopg``), streaming the uploaded CSV
from S3 row-by-row (never loading it whole) and landing contacts in COPY-staged chunks. Because a
staged row may dedupe on *either* partial-unique index, each chunk upserts in **two passes**
mirroring :func:`crm.service.identify`:

- **Pass A** — rows with an ``external_id`` upsert on ``contacts_ext``.
- **Pass B** — rows with only an ``email`` (forced ``kind='user'``) upsert on the email index.

Each pass ``DISTINCT ON`` the conflict key so a duplicate *within one chunk* can't trip
"ON CONFLICT cannot affect row a second time"; across chunks / re-runs the ``ON CONFLICT DO UPDATE``
makes the whole import idempotent (re-running the same file yields zero new contacts — the
acceptance criterion). Pass A deliberately does **not** overwrite ``email`` on conflict: that could
violate the *other* unique index mid-chunk and abort it; cross-key email moves are left to the merge
job (as ``identify`` documents). Malformed rows never fail the job — they go to a bounded S3 error
report and the job still ``completed`` with ``error_rows > 0``.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import psycopg

from relay.core import storage
from relay.core.ids import uuid7
from relay.core.logging import get_logger
from relay.settings import get_settings

log = get_logger(__name__)

CHUNK = 5_000
# Cap the in-report error rows kept/written (memory bound); error_rows still counts them all.
MAX_ERROR_REPORT_ROWS = 10_000
_CORE_TARGETS = ("external_id", "email", "phone", "name", "kind")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class JobNotVisible(Exception):
    """The job row isn't visible yet (created txn not committed) — the task should retry."""


@dataclass
class _ResolvedTarget:
    header: str
    is_custom: bool
    name: str  # core field name, or the custom key


@dataclass
class _Row:
    id: uuid.UUID
    external_id: str | None
    email: str | None
    phone: str | None
    name: str | None
    kind: str | None
    custom: dict[str, Any] = field(default_factory=dict)


def resolve_mapping(mapping: dict[str, str], attr_types: dict[str, str]) -> list[_ResolvedTarget]:
    """Validate the ``{csv_header: target_field}`` mapping against the contact schema.

    Raises ``ValueError`` (→ the job fails fast) on an unknown target, an undefined custom key,
    or when no identity key (``external_id``/``email``) is mapped (nothing could be deduped).
    """
    resolved: list[_ResolvedTarget] = []
    for header, target in mapping.items():
        if target in _CORE_TARGETS:
            resolved.append(_ResolvedTarget(header=header, is_custom=False, name=target))
        elif target.startswith("custom."):
            key = target[len("custom.") :]
            if not key or "." in key:
                raise ValueError(f"invalid custom target {target!r}")
            if key not in attr_types:
                raise ValueError(f"unknown contact attribute {key!r} (define it first)")
            resolved.append(_ResolvedTarget(header=header, is_custom=True, name=key))
        else:
            raise ValueError(f"unknown import target field {target!r}")
    targets = {r.name for r in resolved if not r.is_custom}
    if "external_id" not in targets and "email" not in targets:
        raise ValueError("column mapping must include at least one of: external_id, email")
    return resolved


def _coerce_custom(data_type: str, raw: str) -> Any:
    """Coerce a CSV cell (always a string) to the attribute's declared JSON type. Raises
    ``ValueError`` on a mismatch (→ the row goes to the error report)."""
    if data_type == "string":
        return raw
    if data_type == "number":
        f = float(raw)
        return int(f) if f.is_integer() else f
    if data_type == "boolean":
        low = raw.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
        raise ValueError(f"{raw!r} is not a boolean")
    if data_type == "date":
        dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))  # validate; stored as the ISO string
        return raw
    if data_type == "list":
        return [item.strip() for item in raw.split(",") if item.strip()]
    raise ValueError(f"unsupported attribute type {data_type!r}")


def parse_row(
    raw: dict[str, str | None],
    resolved: list[_ResolvedTarget],
    attr_types: dict[str, str],
    row_number: int,
) -> tuple[_Row | None, tuple[int, str, str] | None]:
    """Map + validate one CSV row. Returns ``(row, None)`` on success or ``(None, error)`` where
    ``error`` is ``(row_number, code, detail)``. An empty cell means "field not provided"."""
    row = _Row(id=uuid7(), external_id=None, email=None, phone=None, name=None, kind=None)
    for target in resolved:
        val = (raw.get(target.header) or "").strip()
        if not val:
            continue
        if target.is_custom:
            try:
                row.custom[target.name] = _coerce_custom(attr_types[target.name], val)
            except ValueError as exc:
                return None, (row_number, "invalid_custom", f"{target.name}: {exc}")
            continue
        if target.name == "kind" and val not in ("user", "lead"):
            return None, (row_number, "invalid_kind", val)
        if target.name == "email" and not _EMAIL_RE.match(val):
            return None, (row_number, "invalid_email", val)
        setattr(row, target.name, val)
    if not row.external_id and not row.email:
        return None, (row_number, "missing_identity", "row has neither external_id nor email")
    return row, None


_STAGE_DDL = (
    "CREATE TEMP TABLE _imp_stage ("
    "id uuid, external_id text, email citext, phone text, name text, kind text, custom jsonb"
    ") ON COMMIT DROP"
)
_COPY_SQL = "COPY _imp_stage (id, external_id, email, phone, name, kind, custom) FROM STDIN"

# Pass A1 — rows carrying an external_id, upsert on contacts_ext. ``email`` is DELIBERATELY NOT
# written here: a bulk INSERT that carried email could have two rows with *different* external_ids
# but the *same* email, and since only ``external_id`` is the conflict arbiter the second row would
# violate the OTHER partial index (contacts_email_user) and abort the whole chunk. Email is filled
# afterwards by the guarded A2 backfill. DISTINCT ON dedups same-external_id rows within the chunk.
_UPSERT_A = """
INSERT INTO contacts (id, workspace_id, kind, external_id, phone, name, custom, created_at)
SELECT DISTINCT ON (s.external_id)
       s.id, %(ws)s, COALESCE(s.kind, 'user'), s.external_id, s.phone, s.name,
       COALESCE(s.custom, '{}'::jsonb), now()
FROM _imp_stage s
WHERE s.external_id IS NOT NULL
ORDER BY s.external_id, s.id DESC
ON CONFLICT (workspace_id, external_id) WHERE (external_id IS NOT NULL AND deleted_at IS NULL)
DO UPDATE SET
    phone  = COALESCE(EXCLUDED.phone, contacts.phone),
    name   = COALESCE(EXCLUDED.name, contacts.name),
    custom = contacts.custom || EXCLUDED.custom,
    kind   = COALESCE(EXCLUDED.kind, contacts.kind)
RETURNING (xmax = 0) AS inserted
"""

# Pass A2 — backfill email onto external_id contacts, but ONLY where it can't collide. DISTINCT ON
# (email) guarantees each email is assigned to at most one external_id in this chunk (so the UPDATE
# can't create an in-statement duplicate), and the NOT EXISTS skips any email already owned by a
# DIFFERENT user contact (existing or just created by Pass B). Colliding emails are left unset —
# the same "cross-key collisions go to the merge job" rule ``identify`` documents. Runs LAST so it
# defers to email-only (Pass B) contacts.
_BACKFILL_A_EMAIL = """
UPDATE contacts c
SET email = s.email
FROM (
    SELECT DISTINCT ON (email) external_id, email
    FROM _imp_stage
    WHERE external_id IS NOT NULL AND email IS NOT NULL
    ORDER BY email, id DESC
) s
WHERE c.workspace_id = %(ws)s
  AND c.external_id = s.external_id
  AND c.email IS DISTINCT FROM s.email
  AND NOT EXISTS (
      SELECT 1 FROM contacts o
      WHERE o.workspace_id = %(ws)s AND o.email = s.email
        AND o.kind = 'user' AND o.deleted_at IS NULL
        AND o.external_id IS DISTINCT FROM s.external_id
  )
"""

# Pass B — email-only rows (forced kind='user' to match the partial index), upsert on
# contacts_email_user. DISTINCT ON email dedups within-chunk duplicates.
_UPSERT_B = """
INSERT INTO contacts (id, workspace_id, kind, email, phone, name, custom, created_at)
SELECT DISTINCT ON (s.email)
       s.id, %(ws)s, 'user', s.email, s.phone, s.name, COALESCE(s.custom, '{}'::jsonb), now()
FROM _imp_stage s
WHERE s.external_id IS NULL AND s.email IS NOT NULL
ORDER BY s.email, s.id DESC
ON CONFLICT (workspace_id, email) WHERE (kind = 'user' AND email IS NOT NULL AND deleted_at IS NULL)
DO UPDATE SET
    phone  = COALESCE(EXCLUDED.phone, contacts.phone),
    name   = COALESCE(EXCLUDED.name, contacts.name),
    custom = contacts.custom || EXCLUDED.custom
RETURNING (xmax = 0) AS inserted
"""


def _land_chunk(conn: psycopg.Connection, workspace_id: str, rows: list[_Row]) -> tuple[int, int]:
    """Land one chunk in a single transaction: temp-stage COPY + two-pass upsert. Returns
    ``(inserted, updated)``."""
    inserted = 0
    updated = 0
    with conn.cursor() as cur:
        cur.execute(_STAGE_DDL)
        with cur.copy(_COPY_SQL) as copy:
            for r in rows:
                copy.write_row(
                    (r.id, r.external_id, r.email, r.phone, r.name, r.kind, json.dumps(r.custom))
                )
        # A1 (external_id identity, no email) → B (email-only) → A2 (guarded email backfill). Only
        # A1 + B create/update contacts, so only they contribute to the inserted/updated counts.
        for sql in (_UPSERT_A, _UPSERT_B):
            cur.execute(sql, {"ws": workspace_id})
            for (is_insert,) in cur.fetchall():
                if is_insert:
                    inserted += 1
                else:
                    updated += 1
        cur.execute(_BACKFILL_A_EMAIL, {"ws": workspace_id})
    conn.commit()
    return inserted, updated


def _load_job(conn: psycopg.Connection, job_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, s3_key, column_mapping FROM import_jobs WHERE id = %s", (job_id,)
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"status": row[0], "s3_key": row[1], "column_mapping": row[2]}


def _load_attr_types(conn: psycopg.Connection) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT name, data_type FROM attribute_definitions WHERE entity = 'contact'")
        return dict(cur.fetchall())


def _set_status(conn: psycopg.Connection, job_id: str, **cols: Any) -> None:
    sets = ", ".join(f"{k} = %({k})s" for k in cols)
    with conn.cursor() as cur:
        cur.execute(f"UPDATE import_jobs SET {sets} WHERE id = %(id)s", {**cols, "id": job_id})
    conn.commit()


def _upload_error_report(
    workspace_pub: str, job_id: str, errors: list[tuple[int, str, str]]
) -> str:
    """Write the (bounded) error rows to a CSV in S3 and return its key."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["row_number", "error_code", "detail"])
    writer.writerows(errors)
    key = f"{storage.import_prefix(workspace_pub)}{job_id}/errors.csv"
    storage.upload_fileobj(
        get_settings().s3_bucket_attachments,
        key,
        io.BytesIO(buf.getvalue().encode("utf-8")),
        content_type="text/csv",
    )
    return key


def run_contact_import(job_id: str, workspace_id: str, workspace_pub: str) -> str:
    """Execute a contact import end to end (sync). Idempotent: safe to re-run (ON CONFLICT upserts),
    so a Celery retry re-processes from scratch. Raises :class:`JobNotVisible` if the job row is not
    yet committed (the task retries). ``status='completed'`` short-circuits a duplicate delivery.
    """
    dsn = get_settings().database_url_psycopg
    now = dt.datetime.now(dt.UTC)
    with psycopg.connect(dsn) as conn:
        conn.autocommit = False
        with conn.cursor() as cur:  # session-level GUC so every txn on this conn is tenant-scoped
            cur.execute("SELECT set_config('app.ws', %s, false)", (workspace_id,))
        conn.commit()

        job = _load_job(conn, job_id)
        if job is None:
            raise JobNotVisible(job_id)
        if job["status"] == "completed":
            return "already_done"

        try:
            attr_types = _load_attr_types(conn)
            resolved = resolve_mapping(job["column_mapping"], attr_types)
        except ValueError as exc:
            _set_status(conn, job_id, status="failed", error=str(exc), finished_at=now)
            return "failed"

        _set_status(conn, job_id, status="processing", started_at=now)

        processed = inserted = updated = error_rows = 0
        errors: list[tuple[int, str, str]] = []
        chunk: list[_Row] = []
        try:
            body = storage.open_read_stream(get_settings().s3_bucket_attachments, job["s3_key"])
            text = io.TextIOWrapper(body, encoding="utf-8-sig", newline="")
            reader = csv.DictReader(text)
            headers = set(reader.fieldnames or [])
            missing = [r.header for r in resolved if r.header not in headers]
            if missing:
                _set_status(
                    conn,
                    job_id,
                    status="failed",
                    error=f"mapped columns not in CSV header: {sorted(missing)}",
                    finished_at=dt.datetime.now(dt.UTC),
                )
                return "failed"

            for i, raw in enumerate(reader, start=1):
                row, err = parse_row(raw, resolved, attr_types, i)
                if err is not None:
                    error_rows += 1
                    if len(errors) < MAX_ERROR_REPORT_ROWS:
                        errors.append(err)
                    continue
                assert row is not None
                chunk.append(row)
                if len(chunk) >= CHUNK:
                    ins, upd = _land_chunk(conn, workspace_id, chunk)
                    inserted += ins
                    updated += upd
                    processed += len(chunk)
                    chunk = []
                    _set_status(
                        conn,
                        job_id,
                        processed_rows=processed,
                        inserted_rows=inserted,
                        updated_rows=updated,
                        error_rows=error_rows,
                    )

            if chunk:
                ins, upd = _land_chunk(conn, workspace_id, chunk)
                inserted += ins
                updated += upd
                processed += len(chunk)

            report_key = _upload_error_report(workspace_pub, job_id, errors) if error_rows else None
            _set_status(
                conn,
                job_id,
                status="completed",
                processed_rows=processed,
                inserted_rows=inserted,
                updated_rows=updated,
                error_rows=error_rows,
                total_rows=processed + error_rows,
                error_report_key=report_key,
                finished_at=dt.datetime.now(dt.UTC),
            )
        except Exception as exc:  # transient (S3/DB) or unexpected → mark failed; retry re-runs
            _set_status(
                conn,
                job_id,
                status="failed",
                error=str(exc)[:500],
                finished_at=dt.datetime.now(dt.UTC),
            )
            raise
        log.info(
            "crm.import.completed",
            job_id=job_id,
            inserted=inserted,
            updated=updated,
            errors=error_rows,
        )
        return "completed"
