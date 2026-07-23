"""Chunk -> diff -> embed -> upsert: the ingestion write path (RFC-003 §3-4).

This is where a source becomes retrievable ``content_chunks``. The load-bearing property is the
**diff**: :func:`index_chunks` compares each new chunk's ``content_hash`` against what is already
stored at that ``(source, locale, emb_version, chunk_index)`` and embeds **only changed/new
chunks** (P1.1 acceptance: "URL re-sync only re-embeds changed chunks"). Everything runs under the
caller's RLS session (``app.ws`` set), so writes are tenant-scoped by construction.

Also here: :func:`reembed_workspace`, the dual-version re-embed migration — write every source at
the new version alongside the old, flip the workspace's active ``emb_version`` (atomic per-tenant
cutover), then drop the old version's rows (RFC-003 §4 "retrieval requires emb_version = current").
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from relay.core.ids import uuid7
from relay.core.logging import get_logger
from relay.modules.knowledge import models
from relay.modules.knowledge.chunking import (
    Chunk,
    chunk_article_body,
    chunk_segments,
    segments_from_text,
)
from relay.modules.knowledge.embeddings import EmbeddingProvider
from relay.modules.knowledge.sources import SourceDocument
from relay.modules.knowledge.vectors import to_vector_literal
from relay.settings import get_settings

log = get_logger(__name__)


@dataclass
class IndexStats:
    embedded: int = 0  # chunks (re-)embedded this pass — the diff's headline number
    unchanged: int = 0  # chunks left untouched (content_hash matched)
    deleted: int = 0  # chunks removed (source shrank)
    total: int = 0  # chunks in the source now


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _rowcount(result: object) -> int:
    """DML affected-row count (``CursorResult.rowcount``) without leaking the concrete type."""
    return int(getattr(result, "rowcount", 0) or 0)


def _embed_text(title: str | None, chunk: Chunk) -> str:
    """The text actually embedded: title + heading breadcrumb + body, so the vector carries the
    document's context (the stored ``content`` stays clean for citation display)."""
    parts = [p for p in (title, chunk.heading_path, chunk.content) if p]
    return "\n".join(parts)


_UPSERT_SQL = text(
    """
    INSERT INTO content_chunks
      (id, workspace_id, source_kind, source_id, locale, audience, title, heading_path,
       chunk_index, content, content_hash, token_count, embedding, emb_version,
       created_at, updated_at)
    VALUES
      (:id, :ws, :sk, :sid, :loc, CAST(:aud AS jsonb), :title, :hp,
       :ci, :content, :chash, :tokens, CAST(:emb AS halfvec), :ver,
       now(), now())
    ON CONFLICT (workspace_id, source_kind, source_id, locale, emb_version, chunk_index)
    DO UPDATE SET
       audience = EXCLUDED.audience,
       title = EXCLUDED.title,
       heading_path = EXCLUDED.heading_path,
       content = EXCLUDED.content,
       content_hash = EXCLUDED.content_hash,
       token_count = EXCLUDED.token_count,
       embedding = EXCLUDED.embedding,
       updated_at = now()
    """
)


async def index_chunks(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    source_kind: str,
    source_id: uuid.UUID,
    locale: str,
    audience: dict[str, Any],
    title: str | None,
    chunks: list[Chunk],
    embedder: EmbeddingProvider,
    emb_version: int,
) -> IndexStats:
    """Upsert ``chunks`` for one source, embedding only those whose content changed."""
    # Current state: chunk_index -> content_hash (this source/locale/version only).
    existing_rows = (
        await session.execute(
            sa.select(models.ContentChunk.chunk_index, models.ContentChunk.content_hash).where(
                models.ContentChunk.workspace_id == workspace_id,
                models.ContentChunk.source_kind == source_kind,
                models.ContentChunk.source_id == source_id,
                models.ContentChunk.locale == locale,
                models.ContentChunk.emb_version == emb_version,
            )
        )
    ).all()
    existing: dict[int, str] = dict(existing_rows)  # type: ignore[arg-type]

    stats = IndexStats(total=len(chunks))
    changed: list[Chunk] = []
    changed_hashes: dict[int, str] = {}
    for chunk in chunks:
        chash = _content_hash(chunk.content)
        changed_hashes[chunk.chunk_index] = chash
        if existing.get(chunk.chunk_index) == chash:
            stats.unchanged += 1
        else:
            changed.append(chunk)

    # Batch-embed ONLY the changed chunks (the diff guarantee).
    if changed:
        vectors = await embedder.embed([_embed_text(title, c) for c in changed])
        for chunk, vector in zip(changed, vectors, strict=True):
            await session.execute(
                _UPSERT_SQL,
                {
                    "id": uuid7(),
                    "ws": workspace_id,
                    "sk": source_kind,
                    "sid": source_id,
                    "loc": locale,
                    "aud": json.dumps(audience),
                    "title": title,
                    "hp": chunk.heading_path,
                    "ci": chunk.chunk_index,
                    "content": chunk.content,
                    "chash": changed_hashes[chunk.chunk_index],
                    "tokens": chunk.token_count,
                    "emb": to_vector_literal(vector),
                    "ver": emb_version,
                },
            )
        stats.embedded = len(changed)

    # Drop chunks past the new tail (source shrank). chunk_index is contiguous 0..N-1.
    if len(chunks) < len(existing):
        result = await session.execute(
            sa.delete(models.ContentChunk).where(
                models.ContentChunk.workspace_id == workspace_id,
                models.ContentChunk.source_kind == source_kind,
                models.ContentChunk.source_id == source_id,
                models.ContentChunk.locale == locale,
                models.ContentChunk.emb_version == emb_version,
                models.ContentChunk.chunk_index >= len(chunks),
            )
        )
        stats.deleted = _rowcount(result)

    log.info(
        "knowledge.index",
        source_kind=source_kind,
        source_id=str(source_id),
        embedded=stats.embedded,
        unchanged=stats.unchanged,
        deleted=stats.deleted,
        total=stats.total,
    )
    return stats


async def delete_source_chunks(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    source_kind: str,
    source_id: uuid.UUID,
) -> int:
    """Remove every chunk for a source (all locales/versions) — unpublish/delete/source removal."""
    result = await session.execute(
        sa.delete(models.ContentChunk).where(
            models.ContentChunk.workspace_id == workspace_id,
            models.ContentChunk.source_kind == source_kind,
            models.ContentChunk.source_id == source_id,
        )
    )
    return _rowcount(result)


def build_source_chunks(documents: list[SourceDocument]) -> list[Chunk]:
    """Chunk a set of ingested documents into one contiguously-indexed chunk list.

    Each document is wrapped in a ``# {title}`` heading so pages stay namespaced under their title
    and chunk indices are stable across a re-sync that changes one page but not the others.
    """
    segments = []
    for doc in documents:
        segments.extend(segments_from_text(f"# {doc.title}\n\n{doc.text}"))
    return chunk_segments(segments)


async def get_active_emb_version(session: AsyncSession, workspace_id: uuid.UUID) -> int:
    """The workspace's active retrieval version (knowledge_settings), or the configured default."""
    version = await session.scalar(
        sa.select(models.KnowledgeSettings.emb_version).where(
            models.KnowledgeSettings.workspace_id == workspace_id
        )
    )
    return version if version is not None else get_settings().embedding_version


async def index_article(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    article: models.Article,
    embedder: EmbeddingProvider,
    emb_version: int | None = None,
) -> IndexStats:
    """(Re-)chunk + embed a published article into ``content_chunks``."""
    version = (
        emb_version
        if emb_version is not None
        else await get_active_emb_version(session, workspace_id)
    )
    chunks = chunk_article_body(article.body)
    return await index_chunks(
        session,
        workspace_id=workspace_id,
        source_kind="article",
        source_id=article.id,
        locale=article.locale,
        audience={},
        title=article.title,
        chunks=chunks,
        embedder=embedder,
        emb_version=version,
    )


async def index_source_documents(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    source: models.ExternalSource,
    documents: list[SourceDocument],
    embedder: EmbeddingProvider,
    emb_version: int | None = None,
) -> IndexStats:
    """(Re-)chunk + embed the documents produced by an external source's sync."""
    version = (
        emb_version
        if emb_version is not None
        else await get_active_emb_version(session, workspace_id)
    )
    chunks = build_source_chunks(documents)
    return await index_chunks(
        session,
        workspace_id=workspace_id,
        source_kind=source.kind,
        source_id=source.id,
        locale=source.locale,
        audience=source.audience,
        title=source.title,
        chunks=chunks,
        embedder=embedder,
        emb_version=version,
    )


async def reembed_workspace(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    new_version: int,
    embedder: EmbeddingProvider,
) -> dict[str, int]:
    """Dual-version re-embed with atomic per-workspace cutover (RFC-003 §4).

    1. Write every published article + external source's chunks at ``new_version`` (alongside the
       old version's rows — retrieval still serves the old version until the flip).
    2. Flip ``knowledge_settings.emb_version`` to ``new_version`` (one row UPDATE = the cutover).
    3. Delete every chunk not at ``new_version`` (old-version cleanup).
    """
    articles = (
        await session.scalars(
            sa.select(models.Article).where(
                models.Article.workspace_id == workspace_id,
                models.Article.status == "published",
                models.Article.deleted_at.is_(None),
            )
        )
    ).all()
    embedded = 0
    for article in articles:
        stats = await index_article(
            session,
            workspace_id=workspace_id,
            article=article,
            embedder=embedder,
            emb_version=new_version,
        )
        embedded += stats.embedded

    # NOTE: external sources are re-embedded from their already-stored chunk content at the new
    # version (no re-fetch): copy each source's current-version chunks forward, re-embedding.
    sources = (
        await session.scalars(
            sa.select(models.ExternalSource).where(
                models.ExternalSource.workspace_id == workspace_id
            )
        )
    ).all()
    current = await get_active_emb_version(session, workspace_id)
    for source in sources:
        embedded += await _reembed_source_from_stored(
            session,
            workspace_id=workspace_id,
            source=source,
            from_version=current,
            new_version=new_version,
            embedder=embedder,
        )

    # Atomic cutover: flip the active version (upsert the settings row).
    await session.execute(
        text(
            """
            INSERT INTO knowledge_settings (id, workspace_id, emb_version, ef_search,
                                            created_at, updated_at)
            VALUES (:id, :ws, :ver, :ef, now(), now())
            ON CONFLICT (workspace_id) DO UPDATE SET emb_version = EXCLUDED.emb_version,
                                                     updated_at = now()
            """
        ),
        {"id": uuid7(), "ws": workspace_id, "ver": new_version, "ef": 100},
    )

    # Old-version cleanup.
    deleted = await session.execute(
        sa.delete(models.ContentChunk).where(
            models.ContentChunk.workspace_id == workspace_id,
            models.ContentChunk.emb_version != new_version,
        )
    )
    return {"embedded": embedded, "deleted": _rowcount(deleted), "new_version": new_version}


async def _reembed_source_from_stored(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    source: models.ExternalSource,
    from_version: int,
    new_version: int,
    embedder: EmbeddingProvider,
) -> int:
    """Re-embed a source's stored chunks at ``new_version`` (no re-fetch of the origin)."""
    rows = (
        await session.execute(
            sa.select(
                models.ContentChunk.chunk_index,
                models.ContentChunk.content,
                models.ContentChunk.content_hash,
                models.ContentChunk.token_count,
                models.ContentChunk.heading_path,
                models.ContentChunk.title,
            ).where(
                models.ContentChunk.workspace_id == workspace_id,
                models.ContentChunk.source_kind == source.kind,
                models.ContentChunk.source_id == source.id,
                models.ContentChunk.locale == source.locale,
                models.ContentChunk.emb_version == from_version,
            )
        )
    ).all()
    if not rows:
        return 0
    texts = ["\n".join(p for p in (r.title, r.heading_path, r.content) if p) for r in rows]
    vectors = await embedder.embed(texts)
    for row, vector in zip(rows, vectors, strict=True):
        await session.execute(
            _UPSERT_SQL,
            {
                "id": uuid7(),
                "ws": workspace_id,
                "sk": source.kind,
                "sid": source.id,
                "loc": source.locale,
                "aud": json.dumps(source.audience),
                "title": row.title,
                "hp": row.heading_path,
                "ci": row.chunk_index,
                "content": row.content,
                "chash": row.content_hash,
                "tokens": row.token_count,
                "emb": to_vector_literal(vector),
                "ver": new_version,
            },
        )
    return len(rows)
