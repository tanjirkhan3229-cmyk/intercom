#!/usr/bin/env python3
"""Migration lint (RFC-002 §9, RFC-001 §13).

Forbids building an index NON-concurrently on a table expected to exceed ~1M rows —
a plain ``CREATE INDEX`` takes an ACCESS EXCLUSIVE lock and would stall writes on the
hottest tables. Such indexes must use ``CREATE INDEX CONCURRENTLY`` (via Alembic's
``op.create_index(..., postgresql_concurrently=True)`` inside an autocommit block, or
raw ``op.execute("CREATE INDEX CONCURRENTLY ...")``).

Checks both ``op.create_index(...)`` AST calls and raw ``op.execute("CREATE INDEX ...")``
strings across apps/api/migrations/versions/*.py. Exit 1 on any violation.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

# Tables projected to cross the ~1M-row threshold at the RFC-000 envelope (RFC-002 §3).
LARGE_TABLES: frozenset[str] = frozenset(
    {
        "conversation_parts",
        "conversations",
        "events",
        "contacts",
        "sends",
        "message_events",
        "webhook_deliveries",
        "audit_logs",
        "content_chunks",
    }
)

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "apps" / "api" / "migrations" / "versions"

_RAW_CREATE_INDEX = re.compile(
    r"create\s+(?:unique\s+)?index\s+(?:concurrently\s+)?.*?\bon\s+(?:only\s+)?\"?(\w+)\"?",
    re.IGNORECASE | re.DOTALL,
)
_HAS_CONCURRENTLY = re.compile(r"create\s+(?:unique\s+)?index\s+concurrently", re.IGNORECASE)


def _const_str(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _check_create_index_call(call: ast.Call, file: Path, violations: list[str]) -> None:
    # op.create_index(index_name, table_name, [columns], ...)
    table = None
    if len(call.args) >= 2:
        table = _const_str(call.args[1])
    for kw in call.keywords:
        if kw.arg == "table_name":
            table = _const_str(kw.value)
    if table is None or table not in LARGE_TABLES:
        return
    concurrently = any(
        kw.arg == "postgresql_concurrently"
        and isinstance(kw.value, ast.Constant)
        and kw.value.value is True
        for kw in call.keywords
    )
    if not concurrently:
        violations.append(
            f"{file.name}:{call.lineno}: op.create_index on large table '{table}' "
            f"must pass postgresql_concurrently=True"
        )


def _check_raw_execute(call: ast.Call, file: Path, violations: list[str]) -> None:
    # op.execute("CREATE INDEX ... ON <large_table> ...") without CONCURRENTLY
    if not call.args:
        return
    sql = _const_str(call.args[0])
    if not sql or "index" not in sql.lower():
        return
    for match in _RAW_CREATE_INDEX.finditer(sql):
        table = match.group(1)
        if table in LARGE_TABLES and not _HAS_CONCURRENTLY.search(sql):
            violations.append(
                f"{file.name}:{call.lineno}: raw CREATE INDEX on large table '{table}' "
                f"must be CREATE INDEX CONCURRENTLY"
            )


def _is_op_call(call: ast.Call, name: str) -> bool:
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == name
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "op"
    )


def check_file(file: Path) -> list[str]:
    violations: list[str] = []
    tree = ast.parse(file.read_text(), filename=str(file))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _is_op_call(node, "create_index"):
            _check_create_index_call(node, file, violations)
        elif _is_op_call(node, "execute"):
            _check_raw_execute(node, file, violations)
    return violations


def main() -> int:
    if not VERSIONS_DIR.exists():
        print(f"migration-lint: no versions dir at {VERSIONS_DIR}", file=sys.stderr)
        return 0
    all_violations: list[str] = []
    for file in sorted(VERSIONS_DIR.glob("*.py")):
        all_violations.extend(check_file(file))
    if all_violations:
        print("migration-lint: FAIL", file=sys.stderr)
        for v in all_violations:
            print(f"  - {v}", file=sys.stderr)
        return 1
    print("migration-lint: OK (no non-concurrent index builds on large tables)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
