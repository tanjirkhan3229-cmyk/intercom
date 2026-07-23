"""The RLS auditor passes against the real migrated schema (RFC-002 §7).

Boots the shared testcontainers Postgres (via the session-scoped ``_database`` fixture, which
runs the real Alembic migrations and exports ``MIGRATION_DATABASE_URL``/``DATABASE_URL``),
then runs ``scripts/audit_rls.py`` as a subprocess. A green run proves every tenant table
created by the migrations has RLS enabled + forced + a policy. PII scrubbing is covered
separately by ``tests/unit/test_scrub.py``.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

# apps/api/tests/integration/test_rls_audit.py -> [2]=api, [3]=apps, [4]=repo root.
REPO_ROOT = Path(__file__).resolve().parents[4]
AUDIT_SCRIPT = REPO_ROOT / "scripts" / "audit_rls.py"


async def test_rls_audit_passes(_database: None) -> None:
    assert AUDIT_SCRIPT.exists(), f"auditor not found at {AUDIT_SCRIPT}"
    result = subprocess.run(  # noqa: ASYNC221 — brief one-shot audit, not an app hot path
        [sys.executable, str(AUDIT_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # Guard against a vacuous pass: the auditor must have actually examined tenant tables.
    match = re.search(r"OK \((\d+) tenant tables", result.stdout)
    assert match is not None, f"unexpected auditor output: {result.stdout}"
    assert int(match.group(1)) > 0, "auditor checked zero tenant tables"
