"""Pure, dependency-free helpers for the ``knowledge`` module.

Kept side-effect-free so they unit-test without a database (a block body → plaintext,
slugs, and listing excerpts). The block body is the editor's JSON
(``{"blocks": [{"type": ..., "text": ...}, ...]}``); the extracted plaintext feeds
``articles.body_text`` → the generated ``search_tsv`` (FTS, R7/R8) and the listing excerpt.
Extraction is deliberately tolerant: unknown block types, missing fields, and malformed
input yield ``""`` rather than raising, so a weird body can never 500 a publish.
"""

from __future__ import annotations

import re
from typing import Any

# Keys within a block whose string values are human-readable text worth indexing.
_TEXT_KEYS: tuple[str, ...] = ("text", "caption", "alt", "code", "title", "header")
# Keys whose values hold nested blocks / inline runs to recurse into.
_CONTAINER_KEYS: tuple[str, ...] = ("blocks", "items", "content", "children", "rows", "cells")
# Guard against a pathological body blowing past Postgres' ~1 MB tsvector limit.
_MAX_TEXT_CHARS = 500_000
_WS_RE = re.compile(r"\s+")
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")


class _Accumulator:
    """Collects extracted strings and stops once the tsvector cap is reached, so an adversarial
    body (millions of tiny strings) cannot spike memory before the final slice."""

    __slots__ = ("parts", "total")

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.total = 0

    @property
    def full(self) -> bool:
        return self.total >= _MAX_TEXT_CHARS

    def add(self, s: str) -> None:
        self.parts.append(s)
        self.total += len(s)


def blocks_to_text(body: Any) -> str:
    """Extract readable plaintext from a block-based article body.

    Walks the structure collecting strings under known text keys and recursing into known
    container keys. Whitespace is collapsed; extraction stops at (and the result is capped to)
    ``_MAX_TEXT_CHARS`` so it always fits a ``tsvector`` and bounds memory.
    """
    acc = _Accumulator()
    _walk(body, acc, depth=0)
    text = _WS_RE.sub(" ", " ".join(acc.parts)).strip()
    return text[:_MAX_TEXT_CHARS]


def _walk(node: Any, acc: _Accumulator, *, depth: int) -> None:
    # Bound recursion (stack) and total length (memory); either limit stops the walk.
    if depth > 64 or acc.full:
        return
    if isinstance(node, str):
        acc.add(node)
        return
    if isinstance(node, list):
        for item in node:
            if acc.full:
                return
            _walk(item, acc, depth=depth + 1)
        return
    if isinstance(node, dict):
        for key in _TEXT_KEYS:
            val = node.get(key)
            if isinstance(val, str):
                acc.add(val)
        for key in _CONTAINER_KEYS:
            if acc.full:
                return
            if key in node:
                _walk(node[key], acc, depth=depth + 1)


def slugify(value: str) -> str:
    """Lowercase, hyphenate, strip to ``[a-z0-9-]``. Mirrors identity's workspace slugify."""
    slug = _SLUG_STRIP_RE.sub("-", value.lower()).strip("-")
    return slug or "untitled"


def excerpt(text: str, limit: int = 200) -> str:
    """A short, word-boundary-aware summary for listing cards and SEO fallback."""
    text = _WS_RE.sub(" ", text).strip()
    if len(text) <= limit:
        return text
    head = text[:limit].rsplit(" ", 1)[0].rstrip()
    return f"{head or text[:limit]}…"
