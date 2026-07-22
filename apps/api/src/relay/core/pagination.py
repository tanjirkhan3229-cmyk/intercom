"""Keyset pagination envelope (RFC-002 §5.1, §6 — keyset only, never OFFSET).

The ``Page`` envelope mirrors the ``@relay/shared`` ``Page<T>`` type used by the web/widget
clients. Cursors are opaque strings minted by each service from its sort key (a public id,
or a ``created_at,id`` tuple for partitioned tables). Hot-path list endpoints paginate by
keyset so cost stays constant as the offset grows.
"""

from __future__ import annotations

from pydantic import BaseModel

# Default and hard-cap page sizes for list endpoints.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


class Page[T](BaseModel):
    """A single keyset page. ``next_cursor`` is ``None`` when the last page is reached."""

    items: list[T]
    next_cursor: str | None = None


def clamp_limit(limit: int | None) -> int:
    """Clamp a client-supplied page size into ``[1, MAX_PAGE_SIZE]`` (default if unset)."""
    if limit is None:
        return DEFAULT_PAGE_SIZE
    return max(1, min(limit, MAX_PAGE_SIZE))
