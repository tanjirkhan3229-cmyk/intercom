"""Ingestion orchestration (P1.1): tie sources + chunking + embedding into re-runnable units.

These are the callables the Celery tasks and the tests both drive (per the codebase convention:
expose the work as a function, invoke it in-process in tests). Each opens its own
``session_scope(workspace_id)`` so it is safe to call from a sync Celery task via the asyncio
bridge, and each is idempotent (re-running re-diffs; only changed chunks re-embed).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from collections.abc import Callable
from typing import Any

from relay.core.db import session_scope
from relay.core.logging import get_logger
from relay.modules.knowledge import indexing, models
from relay.modules.knowledge.embeddings import EmbeddingProvider, get_embedder
from relay.modules.knowledge.sources import (
    Fetcher,
    HttpFetcher,
    NullOcrEngine,
    OcrEngine,
    PdfExtractor,
    PypdfExtractor,
    SourceDocument,
    sync_pdf_source,
    sync_snippet_source,
    sync_url_source,
)
from relay.settings import get_settings

log = get_logger(__name__)

BlobLoader = Callable[[dict[str, Any]], bytes]


async def reindex_article(
    workspace_id: uuid.UUID, article_id: uuid.UUID, *, embedder: EmbeddingProvider | None = None
) -> indexing.IndexStats | None:
    """Re-chunk + embed a published article (outbox-driven, freshness <= minutes). Draft/deleted
    articles are de-indexed instead so retrieval never serves unpublished content."""
    embedder = embedder or get_embedder()
    async with session_scope(workspace_id) as session:
        article = await session.get(models.Article, article_id)
        if article is None or article.status != "published" or article.deleted_at is not None:
            deleted = await indexing.delete_source_chunks(
                session, workspace_id=workspace_id, source_kind="article", source_id=article_id
            )
            log.info("knowledge.article.deindexed", article_id=str(article_id), deleted=deleted)
            return None
        return await indexing.index_article(
            session, workspace_id=workspace_id, article=article, embedder=embedder
        )


async def deindex_article(workspace_id: uuid.UUID, article_id: uuid.UUID) -> int:
    async with session_scope(workspace_id) as session:
        return await indexing.delete_source_chunks(
            session, workspace_id=workspace_id, source_kind="article", source_id=article_id
        )


def _default_blob_loader(config: dict[str, Any]) -> bytes:
    """Load a PDF's bytes from S3 (prod). Tests inject bytes via ``config['_bytes']`` instead."""
    inline = config.get("_bytes")
    if isinstance(inline, bytes):
        return inline
    from relay.core import storage

    bucket = str(config.get("bucket") or get_settings().s3_bucket_attachments)
    return storage.get_object(bucket, str(config["s3_key"]))


async def _collect_documents(
    source: models.ExternalSource,
    *,
    fetcher: Fetcher,
    extractor: PdfExtractor,
    ocr: OcrEngine,
    blob_loader: BlobLoader,
    max_pages: int,
) -> list[SourceDocument]:
    if source.kind == "url":
        return await sync_url_source(source.config, fetcher, max_pages=max_pages)
    if source.kind == "snippet":
        return sync_snippet_source(source.config, title=source.title)
    if source.kind == "pdf":
        data = blob_loader(source.config)
        return sync_pdf_source(data, title=source.title, extractor=extractor, ocr=ocr)
    raise ValueError(f"unknown source kind {source.kind!r}")


def _documents_hash(documents: list[SourceDocument]) -> str:
    h = hashlib.sha256()
    for doc in documents:
        h.update(doc.key.encode("utf-8"))
        h.update(b"\x00")
        h.update(doc.text.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


async def sync_and_index_source(
    *,
    workspace_id: uuid.UUID,
    source_id: uuid.UUID,
    fetcher: Fetcher | None = None,
    extractor: PdfExtractor | None = None,
    ocr: OcrEngine | None = None,
    blob_loader: BlobLoader | None = None,
    embedder: EmbeddingProvider | None = None,
    max_pages: int | None = None,
) -> indexing.IndexStats:
    """Fetch a source, chunk + diff-embed it, and update its AI-readiness status.

    Three phases across two transactions: (1) mark ``syncing`` and commit so the UI reflects it;
    (2) fetch/extract off-DB (bounded, timed out); (3) index (the per-chunk diff re-embeds only
    changed chunks) and mark ``synced``/``error``. Malformed input marks ``error`` and never raises
    past the caller in a way that poisons a queue.
    """
    settings = get_settings()
    fetcher = fetcher or HttpFetcher(
        timeout_seconds=settings.source_fetch_timeout_seconds,
        max_bytes=settings.source_max_document_bytes,
    )
    extractor = extractor or PypdfExtractor()
    ocr = ocr or NullOcrEngine()
    blob_loader = blob_loader or _default_blob_loader
    embedder = embedder or get_embedder()
    max_pages = max_pages or settings.source_crawl_max_pages

    async with session_scope(workspace_id) as session:
        source = await session.get(models.ExternalSource, source_id)
        if source is None:
            raise ValueError(f"source {source_id} not found")
        source.status = "syncing"

    async with session_scope(workspace_id) as session:
        source = await session.get(models.ExternalSource, source_id)
        if source is None:
            raise ValueError(f"source {source_id} not found")
        try:
            documents = await _collect_documents(
                source,
                fetcher=fetcher,
                extractor=extractor,
                ocr=ocr,
                blob_loader=blob_loader,
                max_pages=max_pages,
            )
            stats = await indexing.index_source_documents(
                session,
                workspace_id=workspace_id,
                source=source,
                documents=documents,
                embedder=embedder,
            )
            source.status = "synced"
            source.last_error = None
            source.last_synced_at = dt.datetime.now(dt.UTC)
            source.document_count = len(documents)
            source.chunk_count = stats.total
            source.content_hash = _documents_hash(documents)
            return stats
        except Exception as exc:
            source.status = "error"
            source.last_error = str(exc)[:1000]
            log.warning("knowledge.source.sync_failed", source_id=str(source_id), error=str(exc))
            raise
