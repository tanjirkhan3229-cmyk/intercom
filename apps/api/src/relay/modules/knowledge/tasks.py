"""Celery tasks for the ``knowledge`` module (P1.1) — the embedding/ingestion bulkhead.

The heavy, provider-bound work (chunk + embed + upsert; crawl + extract) runs here on the
``ai.batch`` queue, NOT on the outbox stream drainer or a request path, so a slow embedding
provider or a big crawl can never wedge interactive work (RFC-001 §6.4 bulkheads). Every task is
idempotent: re-running re-diffs against ``content_chunks`` and only changed chunks re-embed.

Async service code is reused verbatim through the per-process asyncio bridge (core/asyncio_bridge).
"""

from __future__ import annotations

import uuid

from relay.core.asyncio_bridge import run_coro
from relay.core.logging import get_logger
from relay.modules.knowledge import indexing, pipeline
from relay.worker import celery_app

log = get_logger(__name__)


@celery_app.task(name="knowledge.reindex_article", queue="ai.batch")
def reindex_article(workspace_id: str, article_id: str) -> dict[str, int]:
    """(Re-)index or de-index one article after a lifecycle event. Idempotent."""
    stats = run_coro(pipeline.reindex_article(uuid.UUID(workspace_id), uuid.UUID(article_id)))
    return {
        "embedded": stats.embedded if stats else 0,
        "unchanged": stats.unchanged if stats else 0,
        "deleted": stats.deleted if stats else 0,
    }


@celery_app.task(name="knowledge.deindex_article", queue="ai.batch")
def deindex_article(workspace_id: str, article_id: str) -> dict[str, int]:
    deleted = run_coro(pipeline.deindex_article(uuid.UUID(workspace_id), uuid.UUID(article_id)))
    return {"deleted": deleted}


@celery_app.task(name="knowledge.sync_source", queue="ai.batch")
def sync_source(workspace_id: str, source_id: str) -> dict[str, int]:
    """Fetch + (re-)index an external source. Only changed chunks re-embed (per-chunk diff)."""
    stats = run_coro(
        pipeline.sync_and_index_source(
            workspace_id=uuid.UUID(workspace_id), source_id=uuid.UUID(source_id)
        )
    )
    return {"embedded": stats.embedded, "unchanged": stats.unchanged, "deleted": stats.deleted}


@celery_app.task(name="knowledge.reembed_workspace", queue="ai.batch")
def reembed_workspace(workspace_id: str, new_version: int) -> dict[str, int]:
    """Dual-version re-embed with atomic per-workspace cutover (RFC-003 §4)."""
    from relay.core.db import session_scope
    from relay.modules.knowledge.embeddings import get_embedder

    async def _run() -> dict[str, int]:
        ws = uuid.UUID(workspace_id)
        async with session_scope(ws) as session:
            return await indexing.reembed_workspace(
                session, workspace_id=ws, new_version=new_version, embedder=get_embedder()
            )

    return run_coro(_run())
