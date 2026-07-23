#!/usr/bin/env python3
"""RLS audit (RFC-002 §7, RFC-001 §10).

Tenancy is enforced by row-level security, not app-layer WHERE clauses. This audit asserts
that **every** tenant table — any base/partitioned table in schema ``public`` carrying a
``workspace_id`` column — has RLS locked down: ENABLED, FORCED (so even the table owner
obeys), and backed by at least one policy (the repo installs ``ws_isolation``).

A "tenant table" mirrors the ``WorkspaceScoped`` mixin (relay.core.base_model): a
**NOT NULL** ``workspace_id`` column — tenancy is mandatory, every row belongs to a
workspace. This discriminator (not a hardcoded exclude list) also excludes infra tables:
those without ``workspace_id`` at all (outbox, idempotency ledgers, dedupe tables) and DLQ
tables like ``channels_ingest_failures`` that carry a *nullable* diagnostic ``workspace_id``
but are written by workers before the workspace is known, so legitimately carry no RLS.

Partition CHILDREN are skipped: RLS is enforced via the partitioned parent, so children
legitimately report ``relrowsecurity=false``.

Reads the DSN from ``MIGRATION_DATABASE_URL`` (falling back to ``DATABASE_URL``) and connects
with psycopg (v3). Exit 1 if any tenant table is misconfigured; else print OK and exit 0.
"""

from __future__ import annotations

import os
import sys

import psycopg

# Base tables (r) and partitioned parents (p); partition children (relispartition) excluded.
_AUDIT_QUERY = """
SELECT c.relname,
       c.relrowsecurity,
       c.relforcerowsecurity,
       EXISTS (
           SELECT 1 FROM pg_policies p
           WHERE p.schemaname = 'public' AND p.tablename = c.relname
       ) AS has_policy,
       -- A policy is only isolating if its predicate actually scopes by the workspace GUC.
       -- Guards against a hand-rolled permissive policy (e.g. USING (true)) that would pass a
       -- bare presence check while providing zero cross-tenant isolation.
       EXISTS (
           SELECT 1 FROM pg_policies p
           WHERE p.schemaname = 'public' AND p.tablename = c.relname
             AND p.qual ILIKE '%current_setting%app.ws%'
             AND p.qual ILIKE '%workspace_id%'
             AND coalesce(p.with_check, p.qual) ILIKE '%current_setting%app.ws%'
       ) AS has_isolating_policy
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relkind IN ('r', 'p')
  AND NOT c.relispartition
  AND EXISTS (
      SELECT 1 FROM pg_attribute a
      WHERE a.attrelid = c.oid
        AND a.attname = 'workspace_id'
        AND a.attnotnull            -- mandatory tenancy (WorkspaceScoped), not diagnostic infra
        AND NOT a.attisdropped
        AND a.attnum > 0
  )
ORDER BY c.relname
"""


def _psycopg_dsn(url: str) -> str:
    """Strip the SQLAlchemy driver suffix so psycopg.connect() accepts the DSN."""
    return url.replace("+asyncpg", "").replace("+psycopg", "")


def _dsn() -> str | None:
    url = os.environ.get("MIGRATION_DATABASE_URL") or os.environ.get("DATABASE_URL")
    return _psycopg_dsn(url) if url else None


def audit(conn: psycopg.Connection) -> tuple[int, list[str]]:
    """Audit tenant tables. Returns ``(tables_checked, offenders)`` — one offender line per
    tenant table that fails an RLS check."""
    offenders: list[str] = []
    checked = 0
    with conn.cursor() as cur:
        cur.execute(_AUDIT_QUERY)
        for relname, rowsecurity, forcerowsecurity, has_policy, has_isolating in cur.fetchall():
            checked += 1
            failures: list[str] = []
            if not rowsecurity:
                failures.append("RLS not ENABLED (relrowsecurity=false)")
            if not forcerowsecurity:
                failures.append("RLS not FORCED (relforcerowsecurity=false)")
            if not has_policy:
                failures.append("no policy in pg_policies")
            elif not has_isolating:
                failures.append(
                    "policy present but not workspace-isolating "
                    "(predicate must scope by current_setting('app.ws') on workspace_id)"
                )
            if failures:
                offenders.append(f"{relname}: {'; '.join(failures)}")
    return checked, offenders


def main() -> int:
    dsn = _dsn()
    if dsn is None:
        print(
            "rls-audit: FAIL (neither MIGRATION_DATABASE_URL nor DATABASE_URL is set)",
            file=sys.stderr,
        )
        return 1
    with psycopg.connect(dsn) as conn:
        checked, offenders = audit(conn)
    # A zero-table run means the discriminator matched nothing (broken query / empty schema) —
    # a security auditor that checks nothing must fail loudly, never pass vacuously.
    if checked == 0:
        print(
            "rls-audit: FAIL (no tenant tables found — audit matched nothing; refusing to pass)",
            file=sys.stderr,
        )
        return 1
    if offenders:
        print("rls-audit: FAIL", file=sys.stderr)
        for offender in offenders:
            print(f"  - {offender}", file=sys.stderr)
        return 1
    print(f"rls-audit: OK ({checked} tenant tables: RLS enabled + forced + a policy)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
