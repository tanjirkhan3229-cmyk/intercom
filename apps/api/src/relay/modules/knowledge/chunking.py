"""Heading-aware semantic chunking (RFC-003 §4).

Turns a document (article blocks, or extracted URL/PDF text) into retrievable chunks of
**400-800 tokens with ~10-15% overlap**, respecting heading boundaries so a chunk stays on one
topic. The token count is a dependency-free estimate (words + punctuation marks) — good enough to
size chunks consistently between prod and CI without pulling a tokenizer + its model download.

Pipeline: ``segments_from_blocks`` / ``segments_from_text`` normalise a source into
:class:`Segment` (heading path + text); :func:`chunk_segments` packs them greedily into
:class:`Chunk` objects with sentence-boundary overlap. Pure and side-effect-free (unit-tested
without a DB).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from relay.modules.knowledge.blocks import blocks_to_text

_TOKEN_RE = re.compile(r"\w+|[^\w\s]")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_WS_RE = re.compile(r"\s+")

# Chunk-size policy (RFC-003 §4). ``TARGET`` is the soft flush point; ``MAX`` the hard cap.
MIN_TOKENS = 400
MAX_TOKENS = 800
TARGET_TOKENS = 600
OVERLAP_RATIO = 0.12  # within the specced 10-15% band


def estimate_tokens(text: str) -> int:
    """Dependency-free token estimate: word tokens + standalone punctuation."""
    return len(_TOKEN_RE.findall(text))


@dataclass(frozen=True)
class Segment:
    """A run of text under a heading breadcrumb (e.g. ("Billing", "Refunds"))."""

    heading_path: tuple[str, ...]
    text: str


@dataclass(frozen=True)
class Chunk:
    chunk_index: int
    content: str
    heading_path: str | None
    token_count: int


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def _heading_str(path: Sequence[str]) -> str | None:
    parts = [p for p in path if p]
    return " > ".join(parts) if parts else None


# --------------------------------------------------------------------------------------------
# Source -> segments
# --------------------------------------------------------------------------------------------
def segments_from_text(text: str) -> list[Segment]:
    """Split plain/markdown text into heading-scoped segments.

    Markdown ``#`` lines set the heading path by level; blank lines separate paragraphs. Text
    from the readability extractor is emitted in exactly this shape.
    """
    segments: list[Segment] = []
    heading_stack: list[tuple[int, str]] = []  # (level, text)
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            body = _clean(" ".join(paragraph))
            if body:
                path = tuple(h for _, h in heading_stack)
                segments.append(Segment(path, body))
            paragraph.clear()

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        m = _MD_HEADING_RE.match(line.strip())
        if m is not None:
            flush_paragraph()
            level = len(m.group(1))
            title = _clean(m.group(2))
            # Pop deeper-or-equal headings, then push this one.
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            if title:
                heading_stack.append((level, title))
            continue
        if not line.strip():
            flush_paragraph()
            continue
        paragraph.append(line.strip())
    flush_paragraph()
    return segments


def segments_from_blocks(body: Any) -> list[Segment]:
    """Convert an article's block body into heading-scoped segments.

    A block is treated as a heading when its ``type`` mentions "head"/"title" (tolerant of the
    P0.8 editor's exact vocabulary); everything else is content text under the current heading.
    Falls back to a single flat segment if the body isn't the expected ``{"blocks": [...]}``.
    """
    blocks = body.get("blocks") if isinstance(body, dict) else None
    if not isinstance(blocks, list):
        flat = _clean(blocks_to_text(body))
        return [Segment((), flat)] if flat else []

    segments: list[Segment] = []
    heading_stack: list[tuple[int, str]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type", "")).lower()
        text = _clean(blocks_to_text(block))
        if not text:
            continue
        if "head" in btype or btype == "title":
            level = int(block.get("level", 1) or 1)
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, text))
        else:
            segments.append(Segment(tuple(h for _, h in heading_stack), text))
    return segments


# --------------------------------------------------------------------------------------------
# Segments -> chunks (greedy packer with sentence-boundary overlap)
# --------------------------------------------------------------------------------------------
def _split_sentences(text: str) -> list[str]:
    return [s for s in (p.strip() for p in _SENTENCE_SPLIT_RE.split(text)) if s]


def _split_oversize(sentence: str, max_tokens: int) -> list[str]:
    """Break a single over-long sentence into <=max_tokens word windows (last resort)."""
    if estimate_tokens(sentence) <= max_tokens:
        return [sentence]
    words = sentence.split()
    out: list[str] = []
    for start in range(0, len(words), max_tokens):
        out.append(" ".join(words[start : start + max_tokens]))
    return out


@dataclass(frozen=True)
class _Unit:
    heading_path: tuple[str, ...]
    text: str
    tokens: int


def _units(segments: Sequence[Segment]) -> list[_Unit]:
    units: list[_Unit] = []
    for seg in segments:
        for sentence in _split_sentences(seg.text):
            for piece in _split_oversize(sentence, MAX_TOKENS):
                units.append(_Unit(seg.heading_path, piece, estimate_tokens(piece)))
    return units


def chunk_segments(
    segments: Sequence[Segment],
    *,
    min_tokens: int = MIN_TOKENS,
    max_tokens: int = MAX_TOKENS,
    target_tokens: int = TARGET_TOKENS,
    overlap_ratio: float = OVERLAP_RATIO,
) -> list[Chunk]:
    """Pack segments into 400-800-token chunks with sentence-boundary overlap.

    Greedy with guaranteed forward progress: each chunk is a window of sentence units ending at a
    heading change (once past ``min_tokens``) or the token target/cap; the next window backs up by
    ~``overlap_ratio`` of the cap so adjacent chunks share context.
    """
    units = _units(segments)
    if not units:
        return []
    overlap_tokens = max(1, int(max_tokens * overlap_ratio))

    chunks: list[Chunk] = []
    start = 0
    n = len(units)
    while start < n:
        end = start
        tok = 0
        while end < n:
            if (
                end > start
                and units[end].heading_path != units[end - 1].heading_path
                and tok >= min_tokens
            ):
                break
            if end > start and tok + units[end].tokens > max_tokens:
                break
            tok += units[end].tokens
            end += 1
            if tok >= target_tokens:
                break

        window = units[start:end]
        content = _clean(" ".join(u.text for u in window))
        chunks.append(
            Chunk(
                chunk_index=len(chunks),
                content=content,
                heading_path=_heading_str(window[0].heading_path),
                token_count=estimate_tokens(content),
            )
        )
        if end >= n:
            break
        # Back up by ~overlap tokens for the next window, but always advance at least one unit.
        back = 0
        s = end
        while s > start + 1 and back < overlap_tokens:
            back += units[s - 1].tokens
            s -= 1
        start = max(s, start + 1)
    return chunks


def chunk_article_body(body: Any) -> list[Chunk]:
    return chunk_segments(segments_from_blocks(body))


def chunk_plain_text(text: str) -> list[Chunk]:
    return chunk_segments(segments_from_text(text))
