"""pgvector ``halfvec`` glue for the retrieval layer (RFC-002 §5.5).

Embeddings are stored as ``halfvec(1536)`` — pgvector half-precision, which halves the
index/RAM footprint at negligible recall cost (RFC-002 §5.5 rationale). We deliberately do
**not** depend on the ``pgvector`` Python package's asyncpg codec: under our RLS + raw-SQL
retrieval path (Appendix B) it is simpler and pooler-safe to pass vectors as text literals
cast to ``halfvec`` in SQL (``:qvec::halfvec``). This module holds the two primitives that
need to agree everywhere: the SQLAlchemy column type (so ``Base.metadata`` is coherent) and
the ``[a,b,c]`` literal encoder used by both writes and query binds.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy.types import UserDefinedType

# The single source of truth for the embedding width. RFC-002 §5.5 fixes it at 1536; a change
# is an ``emb_version`` migration, never an in-place edit.
EMBEDDING_DIM = 1536


def to_vector_literal(values: Sequence[float]) -> str:
    """Encode a float vector as the ``[a,b,c]`` text pgvector accepts for a ``halfvec`` cast.

    Used for both INSERTs (``VALUES (:emb::halfvec)``) and the query vector bind in retrieval.
    ``repr``-free formatting keeps the payload compact and deterministic.
    """
    return "[" + ",".join(f"{float(v):.6g}" for v in values) + "]"


class Halfvec(UserDefinedType[Any]):
    """Minimal ``halfvec(n)`` column type.

    Renders the DDL/casts; binding/reading go through text literals (see module docstring),
    so we don't register an asyncpg codec. Writes bind a pre-encoded literal via
    :func:`to_vector_literal`; reads come back as text and are parsed where needed.
    """

    cache_ok = True

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self.dim = dim

    def get_col_spec(self, **_kw: Any) -> str:
        return f"halfvec({self.dim})"
