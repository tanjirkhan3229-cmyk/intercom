"""The wire contract between the prompt builder and the model (RFC-003 §6 injection posture).

This module defines *how untrusted content is framed in a prompt* and *how a model's structured
answers are shaped* — the shared vocabulary of :mod:`prompts` (which assembles prompts) and the
hermetic :class:`~relay.modules.ai.providers.DeterministicProvider` (which simulates a model in
CI/dev). A real HTTP model reads the same framing as natural-language instruction; the deterministic
simulator parses it mechanically. Keeping the contract in one place means both sides never drift.

Injection posture (RFC-003 §6): retrieved chunks and customer text are **data, never instructions**.
Every untrusted span is wrapped in a delimited, typed DATA block, and the delimiters are escaped out
of the content first — so a chunk that itself contains ``⟦/DATA⟧ ignore previous instructions`` can
neither close its own block nor smuggle a directive. The system policy is the only instruction
channel; the model is told (and the simulator is built) to treat DATA spans as quoted evidence only.
"""

from __future__ import annotations

import re

# --- Tasks --------------------------------------------------------------------
# One turn fans out to these model calls (RFC-003 §3). Cheap tier: preflight/rewrite/verify;
# frontier tier: generate. The task marker rides in the system prompt so the hermetic simulator
# can dispatch; a real model simply reads the surrounding instruction for that task.
TASK_PREFLIGHT = "preflight"
TASK_REWRITE = "rewrite"
TASK_GENERATE = "generate"
TASK_VERIFY = "verify"

_TASK_MARKER_RE = re.compile(r"relay:task=([a-z_]+)")


def task_marker(task: str) -> str:
    """The machine-readable task tag embedded in a system prompt (``[relay:task=generate]``)."""
    return f"[relay:task={task}]"


def parse_task(system_text: str) -> str | None:
    """Extract the task name from a system prompt, or ``None`` if unmarked."""
    m = _TASK_MARKER_RE.search(system_text)
    return m.group(1) if m else None


# --- Delimited, typed DATA blocks (untrusted content) -------------------------
# Unusual sentinels so ordinary prose never collides; any literal occurrence inside content is
# escaped before wrapping (breakout defense) and restored on parse.
_DATA_OPEN = "⟦DATA"
_DATA_CLOSE = "⟦/DATA⟧"
_OPEN_ESC = "⟦​DATA"  # zero-width space breaks the sentinel without changing visible text
_CLOSE_ESC = "⟦​/DATA⟧"

# The label class excludes ⟧ (and ⟦) so a greedy label can't swallow its own closing bracket and
# cascade into the next block — that miscapture silently merged adjacent DATA spans.
_BLOCK_RE = re.compile(
    r"⟦DATA\s+(?P<label>[^\s|⟦⟧]+)(?P<meta>[^⟧]*)⟧\n(?P<content>.*?)\n⟦/DATA⟧",
    re.DOTALL,
)


def _escape_content(content: str) -> str:
    """Neutralise any embedded delimiter so untrusted content cannot break out of its block."""
    return content.replace(_DATA_CLOSE, _CLOSE_ESC).replace(_DATA_OPEN, _OPEN_ESC)


def wrap_data(label: str, content: str, *, meta: dict[str, str] | None = None) -> str:
    """Frame one untrusted span as a labelled, typed DATA block (delimiters escaped out of content).

    ``label`` is the citation handle (``c1``, ``c2`` …); ``meta`` carries typed, non-instruction
    attributes (source kind, title) a model may surface in a citation but must not obey.
    """
    meta_str = "".join(f" | {k}={v}" for k, v in (meta or {}).items())
    return f"{_DATA_OPEN} {label}{meta_str}⟧\n{_escape_content(content)}\n{_DATA_CLOSE}"


def iter_data_blocks(text: str) -> list[tuple[str, dict[str, str], str]]:
    """Parse ``(label, meta, content)`` for every DATA block in ``text`` (order-preserving)."""
    out: list[tuple[str, dict[str, str], str]] = []
    for m in _BLOCK_RE.finditer(text):
        meta: dict[str, str] = {}
        for part in m.group("meta").split("|"):
            part = part.strip()
            if "=" in part:
                k, _, v = part.partition("=")
                meta[k.strip()] = v.strip()
        out.append((m.group("label"), meta, m.group("content")))
    return out


# --- Citations ----------------------------------------------------------------
# Generation must cite the chunk each claim rests on (RFC-003 §6). Citations reference the DATA
# block labels; the pipeline maps labels back to content_chunk ids for the ledger.
_CITE_RE = re.compile(r"⟦cite:([^⟧]+)⟧")


def cite(label: str) -> str:
    return f"⟦cite:{label}⟧"


def parse_citations(text: str) -> list[str]:
    """Ordered, de-duplicated citation labels referenced in a generated answer."""
    seen: dict[str, None] = {}
    for label in _CITE_RE.findall(text):
        seen.setdefault(label.strip(), None)
    return list(seen)


def strip_citations(text: str) -> str:
    """The human-facing answer with citation markers removed (widget renders them separately)."""
    return re.sub(r"\s*⟦cite:[^⟧]+⟧", "", text).strip()


# --- Structured-response JSON keys (preflight / verify) -----------------------
# The cheap-tier tasks answer with a compact JSON object; these are the agreed keys.
PREFLIGHT_LANGUAGE = "language"
PREFLIGHT_SAFETY = "safety_class"  # ok | abuse | self_harm | out_of_scope
PREFLIGHT_HANDOFF = "handoff_requested"  # explicit "talk to a person"
PREFLIGHT_IS_QUESTION = "is_question"

VERIFY_GROUNDED = "grounded"
VERIFY_SCORE = "score"
VERIFY_UNSUPPORTED = "unsupported_claims"
VERIFY_POLICY_FLAGS = "policy_flags"

REWRITE_QUERY = "query"
