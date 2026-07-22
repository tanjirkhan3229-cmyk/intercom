"""Architecture gates run as ordinary tests (also enforced in CI).

- import-linter contracts hold (no module reaches into another's internals).
- migration lint holds (no non-concurrent index builds on large tables).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

API_DIR = Path(__file__).resolve().parents[2]  # apps/api
REPO_ROOT = API_DIR.parents[1]  # repo root


def test_import_linter_contracts_hold() -> None:
    """`lint-imports` must report all contracts kept."""
    if shutil.which("lint-imports") is None:
        pytest.skip("import-linter not installed")
    result = subprocess.run(
        ["lint-imports", "--config", ".importlinter"],
        cwd=API_DIR,
        env={"PYTHONPATH": "src", "PATH": _path()},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"import-linter failed:\n{result.stdout}\n{result.stderr}"
    assert "0 broken" in result.stdout


def test_migration_lint_holds() -> None:
    """No migration builds an index non-concurrently on a >1M-row table."""
    script = REPO_ROOT / "scripts" / "check_migrations.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"migration lint failed:\n{result.stdout}\n{result.stderr}"


def _path() -> str:
    import os

    return os.environ.get("PATH", "")
