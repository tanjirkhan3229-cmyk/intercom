"""Row-level-security helpers for migrations (RFC-002 §7, §10).

Every tenant-owned table is created through :func:`create_tenant_table`, which enables +
**FORCES** RLS and installs the canonical ``ws_isolation`` policy. The policy reads the
per-transaction GUC ``app.ws`` (set by the session layer). We use
``current_setting('app.ws', true)`` — the ``true`` means "missing_ok": when the GUC is
unset the function returns NULL, so ``workspace_id = NULL`` is NULL → the row is filtered
out. That is why a query with **no** ``app.ws`` returns zero rows instead of erroring —
the defense-in-depth backstop behind the app-layer filter.

FORCE RLS makes even the table owner obey the policy; the ``migrator`` role bypasses it
(BYPASSRLS) for migrations/backfills only.
"""

from __future__ import annotations

from typing import Any

from alembic import op

from relay.core.db import WORKSPACE_GUC

POLICY_NAME = "ws_isolation"


def enable_ws_rls(table_name: str, *, workspace_column: str = "workspace_id") -> None:
    """Enable + force RLS on an existing table and install the ws_isolation policy."""
    guc = WORKSPACE_GUC
    # NULLIF(..., '') is essential: once the placeholder GUC has been touched in a session,
    # current_setting(name, true) returns '' (not NULL) after it's cleared, and ''::uuid
    # would *error*. NULLIF maps both "never set" and "empty" to NULL, so an unset GUC
    # yields `workspace_id = NULL` → NULL → zero rows (never a crash).
    predicate = f"{workspace_column} = NULLIF(current_setting('{guc}', true), '')::uuid"
    op.execute(f'ALTER TABLE "{table_name}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table_name}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY {POLICY_NAME} ON "{table_name}" '
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )


def disable_ws_rls(table_name: str) -> None:
    """Reverse :func:`enable_ws_rls` (for downgrades)."""
    op.execute(f'DROP POLICY IF EXISTS {POLICY_NAME} ON "{table_name}"')
    op.execute(f'ALTER TABLE "{table_name}" NO FORCE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table_name}" DISABLE ROW LEVEL SECURITY')


def create_tenant_table(
    table_name: str,
    *columns: Any,
    workspace_column: str = "workspace_id",
    **kw: Any,
) -> None:
    """``op.create_table`` + automatic RLS. Use this for EVERY tenant-owned table.

    The table must include the ``workspace_column`` (use the ``WorkspaceScoped`` model
    mixin). Grants to app_rw/app_ro come from the migrator's default privileges (infra
    role setup), so nothing extra is needed here.
    """
    op.create_table(table_name, *columns, **kw)
    enable_ws_rls(table_name, workspace_column=workspace_column)
