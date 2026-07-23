"""Hybrid retrieval — the substrate Neko (RFC-003) reads (RFC-002 Appendix B).

``retrieve()`` runs the reciprocal-rank-fusion (RRF) query from RFC-002 Appendix B: a vector-ANN
arm (pgvector HNSW over ``halfvec``) and an FTS arm (Postgres ``websearch_to_tsquery``), each
oversampled to top-N, fused by ``1/(60+rank)``. Filters: workspace (hard — RLS-backed AND an
explicit predicate), locale, ``emb_version = active`` (so a re-embed cuts over atomically), and
optional source kinds / audience. ``ef_search`` is tunable per call (HNSW recall knob).

Tenant isolation is not this module's job to enforce and it cannot bypass it: every query runs in
the caller's ``session_scope(workspace_id)`` transaction, so forced RLS makes another tenant's
chunks unreadable even if the explicit workspace predicate were dropped (proven in the cross-tenant
adversarial test). The three ``method`` variants (hybrid / vector / fts) exist so the eval harness
can show hybrid beats each single-signal baseline.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from relay.modules.knowledge.embeddings import EmbeddingProvider, get_embedder
from relay.modules.knowledge.indexing import get_active_emb_version
from relay.modules.knowledge.models import CHUNK_SOURCE_KINDS
from relay.modules.knowledge.vectors import to_vector_literal
from relay.settings import get_settings

RetrievalMethod = Literal["hybrid", "vector", "fts"]


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: uuid.UUID
    source_id: uuid.UUID
    source_kind: str
    content: str
    title: str | None
    heading_path: str | None
    score: float


# Shared per-arm SELECT bodies (filters interpolated by _filters). ``{src}``/``{aud}`` are safe,
# fixed fragments chosen from validated inputs — never raw user text.
_VECTOR_ARM = """
    SELECT id, source_id, source_kind, content, title, heading_path,
           embedding <=> CAST(:qvec AS halfvec) AS dist
    FROM content_chunks
    WHERE workspace_id = :ws AND locale = :loc AND emb_version = :ver
          AND embedding IS NOT NULL {src} {aud}
    ORDER BY embedding <=> CAST(:qvec AS halfvec)
    LIMIT :oversample
"""
_FTS_ARM = """
    SELECT id, source_id, source_kind, content, title, heading_path,
           ts_rank(tsv, websearch_to_tsquery('simple', :q)) AS r
    FROM content_chunks
    WHERE workspace_id = :ws AND locale = :loc AND emb_version = :ver
          AND tsv @@ websearch_to_tsquery('simple', :q) {src} {aud}
    ORDER BY r DESC
    LIMIT :oversample
"""


def _filters(source_kinds: list[str] | None, audience: dict[str, Any] | None) -> tuple[str, str]:
    src = "AND source_kind = ANY(:kinds)" if source_kinds else ""
    aud = "AND audience @> CAST(:aud AS jsonb)" if audience else ""
    return src, aud


def _build_sql(method: RetrievalMethod, src: str, aud: str) -> str:
    if method == "vector":
        return f"""
        WITH v AS ({_VECTOR_ARM.format(src=src, aud=aud)})
        SELECT id, source_id, source_kind, content, title, heading_path,
               1.0 / (1.0 + dist) AS score
        FROM v ORDER BY dist LIMIT :k
        """
    if method == "fts":
        return f"""
        WITH t AS ({_FTS_ARM.format(src=src, aud=aud)})
        SELECT id, source_id, source_kind, content, title, heading_path, r AS score
        FROM t ORDER BY r DESC LIMIT :k
        """
    # hybrid — RFC-002 Appendix B (RRF fusion of both arms).
    return f"""
    WITH v AS ({_VECTOR_ARM.format(src=src, aud=aud)}),
         t AS ({_FTS_ARM.format(src=src, aud=aud)})
    SELECT id, source_id, source_kind, content, title, heading_path,
           coalesce(1.0 / (60 + vr.rank), 0) + coalesce(1.0 / (60 + tr.rank), 0) AS score
    FROM (SELECT *, row_number() OVER (ORDER BY dist) AS rank FROM v) vr
    FULL JOIN (SELECT *, row_number() OVER (ORDER BY r DESC) AS rank FROM t) tr
         USING (id, source_id, source_kind, content, title, heading_path)
    ORDER BY score DESC LIMIT :k
    """


async def retrieve(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    query: str,
    locale: str = "en",
    k: int | None = None,
    ef_search: int | None = None,
    source_kinds: list[str] | None = None,
    audience: dict[str, Any] | None = None,
    method: RetrievalMethod = "hybrid",
    emb_version: int | None = None,
    embedder: EmbeddingProvider | None = None,
) -> list[RetrievedChunk]:
    """Retrieve the top-``k`` chunks for ``query`` under the current RLS session."""
    settings = get_settings()
    k = k or settings.retrieval_default_k
    ef = int(ef_search or settings.retrieval_default_ef_search)
    oversample = max(k, settings.retrieval_oversample)
    version = (
        emb_version
        if emb_version is not None
        else await get_active_emb_version(session, workspace_id)
    )
    embedder = embedder or get_embedder(settings)

    if source_kinds is not None:
        bad = [k_ for k_ in source_kinds if k_ not in CHUNK_SOURCE_KINDS]
        if bad:
            raise ValueError(f"unknown source_kinds: {bad}")

    qvec = (await embedder.embed([query]))[0]
    src, aud = _filters(source_kinds, audience)
    sql = _build_sql(method, src, aud)

    params: dict[str, Any] = {
        "ws": workspace_id,
        "loc": locale,
        "ver": version,
        "qvec": to_vector_literal(qvec),
        "q": query,
        "oversample": oversample,
        "k": k,
    }
    if source_kinds:
        params["kinds"] = source_kinds
    if audience:
        import json

        params["aud"] = json.dumps(audience)

    # HNSW recall knob — transaction-local so it never leaks to other queries on this connection.
    if method in ("hybrid", "vector"):
        await session.execute(text(f"SET LOCAL hnsw.ef_search = {ef}"))

    rows = (await session.execute(text(sql), params)).mappings().all()
    return [
        RetrievedChunk(
            chunk_id=row["id"],
            source_id=row["source_id"],
            source_kind=row["source_kind"],
            content=row["content"],
            title=row["title"],
            heading_path=row["heading_path"],
            score=float(row["score"]),
        )
        for row in rows
    ]
