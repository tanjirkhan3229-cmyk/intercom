#!/usr/bin/env python3
"""Secret scan (RFC-001 §13: "no secrets in code").

Scans every git-tracked file (``git ls-files``) for likely leaked credentials: AWS access
key IDs, PEM private-key blocks, and high-entropy ``secret``/``password``/``token``/``api_key``
assignments. Real secrets live in AWS Secrets Manager / env, never baked into the tree.

An allowlist keeps dev placeholders and non-source files from tripping the scan: the
committed ``.env.example`` (dev-only sample values), everything under ``docs/``, test files,
and any value that reads as an obvious placeholder (``dev-``, ``change-me``, ``example`` …).
Binary blobs and lockfiles are skipped. Exit 1 on any real finding; else print OK and exit 0.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# --- Detectors -------------------------------------------------------------------------
_AWS_KEY = re.compile(r"AKIA[0-9A-Z]{16}")
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_ASSIGNMENT = re.compile(
    r"""(?ix)
    \b(?:secret|password|passwd|token|api[_-]?key)\b   # sensitive-looking name
    \s*[:=]\s*                                          # assignment / mapping
    (?P<quote>['"])                                     # opening quote
    (?P<value>[^'"]{20,})                               # 20+ chars, no quote
    (?P=quote)                                          # matching close quote
    """
)

# --- Allowlist -------------------------------------------------------------------------
# Placeholder markers: if the matched value contains any of these, it is dev/sample data.
_PLACEHOLDER_MARKERS = (
    "dev-",
    "change-me",
    "changeme",
    "example",
    "placeholder",
    "test-",
    "xxxx",
)
# Paths (relative to repo root) exempt from scanning entirely.
_ALLOWLISTED_FILES = {".env.example"}
_ALLOWLISTED_NAMES = {"package-lock.json", "uv.lock"}
_ALLOWLISTED_SUFFIXES = {".docx"}


def _is_allowlisted_path(rel_path: str) -> bool:
    if rel_path in _ALLOWLISTED_FILES:
        return True
    path = Path(rel_path)
    if path.name in _ALLOWLISTED_NAMES:
        return True
    if path.suffix in _ALLOWLISTED_SUFFIXES:
        return True
    parts = path.parts
    if parts and parts[0] == "docs":
        return True
    return "tests" in parts


def _is_placeholder(value: str) -> bool:
    low = value.lower()
    return any(marker in low for marker in _PLACEHOLDER_MARKERS)


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def scan_file(rel_path: str) -> list[str]:
    findings: list[str] = []
    try:
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return findings  # binary or unreadable — skip
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _AWS_KEY.search(line):
            findings.append(f"{rel_path}:{lineno}: AWS access key ID")
        if _PRIVATE_KEY.search(line):
            findings.append(f"{rel_path}:{lineno}: private key block")
        match = _ASSIGNMENT.search(line)
        if match and not _is_placeholder(match.group("value")):
            findings.append(f"{rel_path}:{lineno}: high-entropy secret assignment")
    return findings


def main() -> int:
    all_findings: list[str] = []
    for rel_path in _tracked_files():
        if _is_allowlisted_path(rel_path):
            continue
        all_findings.extend(scan_file(rel_path))
    if all_findings:
        print("secret-scan: FAIL", file=sys.stderr)
        for finding in all_findings:
            print(f"  - {finding}", file=sys.stderr)
        return 1
    print("secret-scan: OK (no leaked secrets in git-tracked files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
