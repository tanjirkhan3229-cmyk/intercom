"""Integration tests for the ingestion pipeline (P1.1): publish->index, re-sync diff, re-embed.

Covers the P1.1 acceptance points that live in the write path:
- an article publish re-chunks into ``content_chunks`` (and unpublish de-indexes);
- a re-sync re-embeds **only changed chunks** (the diff);
- a re-embed does a dual-version write + atomic per-workspace cutover + old-version cleanup.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import func, select

from relay.core.db import session_scope
from relay.core.ids import IdPrefix, decode_public_id, uuid7
from relay.modules.knowledge import indexing, pipeline
from relay.modules.knowledge.chunking import Chunk
from relay.modules.knowledge.embeddings import DeterministicEmbedder
from relay.modules.knowledge.models import ContentChunk, ExternalSource, KnowledgeSettings
from relay.modules.knowledge.retrieval import retrieve
from relay.modules.knowledge.sources import FetchResult

pytestmark = pytest.mark.integration

PASSWORD = "correct-horse-battery-staple"


class SpyEmbedder:
    """Wraps the deterministic embedder and records every batch of texts it is asked to embed."""

    def __init__(self) -> None:
        self._inner = DeterministicEmbedder()
        self.model = self._inner.model
        self.dimension = self._inner.dimension
        self.batches: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        return await self._inner.embed(texts)

    @property
    def embedded_texts(self) -> list[str]:
        return [t for batch in self.batches for t in batch]


async def _signup(client, name: str):
    resp = await client.post(
        "/v0/auth/signup",
        json={
            "workspace_name": name,
            "email": f"owner-{uuid4().hex}@example.com",
            "password": PASSWORD,
            "name": "Owner",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    return headers, decode_public_id(IdPrefix.WORKSPACE, body["workspace"]["id"])


async def _publish_article(client, headers) -> str:
    para = "You can request a refund from the billing settings within thirty days of purchase. " * 6
    body = {
        "blocks": [
            {"type": "heading", "level": 1, "text": "Refund Policy"},
            {"type": "paragraph", "text": para},
        ]
    }
    resp = await client.post(
        "/v0/articles", json={"title": "Refund Policy", "body": body}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    art_pub = resp.json()["id"]
    pub = await client.post(f"/v0/articles/{art_pub}/publish", headers=headers)
    assert pub.status_code == 200, pub.text
    return art_pub


async def _chunk_count(ws, source_id) -> int:
    async with session_scope(ws) as session:
        return await session.scalar(
            select(func.count())
            .select_from(ContentChunk)
            .where(ContentChunk.source_id == source_id)
        )


async def test_article_publish_indexes_then_unpublish_deindexes(client) -> None:
    headers, ws = await _signup(client, "kb-pub")
    art_pub = await _publish_article(client, headers)
    art_id = decode_public_id(IdPrefix.ARTICLE, art_pub)

    stats = await pipeline.reindex_article(ws, art_id)  # what the indexing consumer/task does
    assert stats is not None and stats.embedded > 0
    assert await _chunk_count(ws, art_id) == stats.total > 0

    # Retrieval finds it.
    async with session_scope(ws) as session:
        results = await retrieve(session, workspace_id=ws, query="how do I get a refund", k=5)
    assert any(r.source_id == art_id for r in results)

    # Unpublish -> the consumer de-indexes.
    await client.post(f"/v0/articles/{art_pub}/unpublish", headers=headers)
    await pipeline.reindex_article(ws, art_id)
    assert await _chunk_count(ws, art_id) == 0


async def test_index_chunks_reembeds_only_changed(client) -> None:
    """The core diff guarantee, isolated from overlap: change one chunk, only that one re-embeds."""
    _headers, ws = await _signup(client, "kb-diff")
    source_id = uuid7()
    v1 = [
        Chunk(0, "Alpha section about invoices and billing cycles.", None, 8),
        Chunk(1, "Bravo section about refunds and reimbursements.", None, 8),
        Chunk(2, "Charlie section about shipping and delivery.", None, 8),
    ]
    async with session_scope(ws) as session:
        s1 = await index_chunks_helper(session, ws, source_id, v1, SpyEmbedder())
    assert s1.embedded == 3 and s1.unchanged == 0

    v2 = list(v1)
    v2[1] = Chunk(1, "Bravo section REVISED: cancellations and money back.", None, 8)
    spy = SpyEmbedder()
    async with session_scope(ws) as session:
        s2 = await index_chunks_helper(session, ws, source_id, v2, spy)
    assert s2.embedded == 1
    assert s2.unchanged == 2
    assert len(spy.embedded_texts) == 1
    assert "REVISED" in spy.embedded_texts[0]


async def index_chunks_helper(session, ws, source_id, chunks, embedder):
    return await indexing.index_chunks(
        session,
        workspace_id=ws,
        source_kind="article",
        source_id=source_id,
        locale="en",
        audience={},
        title=None,
        chunks=chunks,
        embedder=embedder,
        emb_version=1,
    )


def _sitemap(urls: list[str]) -> str:
    locs = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</urlset>'


def _page(title: str, topic: str, n: int = 75) -> str:
    # Real sentences (periods) so the chunker splits them; ~n*12 tokens => several chunks/page.
    body = " ".join(
        f"The {topic} guide detail number {i} explains how the {title} process works end to end."
        for i in range(n)
    )
    return (
        f"<html><head><title>{title}</title></head>"
        f"<body><main><h1>{title}</h1><p>{body}</p></main></body></html>"
    )


class DictFetcher:
    def __init__(self, pages: dict[str, FetchResult]) -> None:
        self._pages = pages

    async def fetch(self, url: str):
        return self._pages.get(url)


async def test_url_resync_reembeds_subset(client) -> None:
    """A realistic URL re-sync where one page changed re-embeds a subset, not the whole source."""
    headers, ws = await _signup(client, "kb-resync")
    resp = await client.post(
        "/v0/sources",
        json={"kind": "url", "title": "Docs", "config": {"url": "https://ex.com/"}},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    source_id = decode_public_id(IdPrefix.EXTERNAL_SOURCE, resp.json()["id"])

    urls = ["https://ex.com/a", "https://ex.com/b", "https://ex.com/c"]

    def fetcher(page_b_sentence: str) -> DictFetcher:
        return DictFetcher(
            {
                "https://ex.com/sitemap.xml": FetchResult(
                    "https://ex.com/sitemap.xml", 200, "application/xml", _sitemap(urls)
                ),
                "https://ex.com/a": FetchResult(
                    "https://ex.com/a", 200, "text/html", _page("Alpha", "invoices and billing")
                ),
                "https://ex.com/b": FetchResult(
                    "https://ex.com/b", 200, "text/html", _page("Bravo", page_b_sentence)
                ),
                "https://ex.com/c": FetchResult(
                    "https://ex.com/c", 200, "text/html", _page("Charlie", "shipping and delivery")
                ),
            }
        )

    spy1 = SpyEmbedder()
    s1 = await pipeline.sync_and_index_source(
        workspace_id=ws, source_id=source_id, fetcher=fetcher("refunds and returns"), embedder=spy1
    )
    assert s1.embedded == s1.total > 3

    spy2 = SpyEmbedder()
    s2 = await pipeline.sync_and_index_source(
        workspace_id=ws,
        source_id=source_id,
        fetcher=fetcher("cancellations and money back now"),  # page B changed only
        embedder=spy2,
    )
    assert s2.unchanged >= 1  # unchanged pages were not re-embedded
    assert s2.embedded < s2.total  # NOT the whole source

    async with session_scope(ws) as session:
        src = await session.get(ExternalSource, source_id)
    assert src.status == "synced"
    assert src.document_count == 3


async def test_reembed_workspace_cutover(client) -> None:
    headers, ws = await _signup(client, "kb-reembed")
    art_pub = await _publish_article(client, headers)
    art_id = decode_public_id(IdPrefix.ARTICLE, art_pub)
    await pipeline.reindex_article(ws, art_id)  # v1 chunks

    async with session_scope(ws) as session:
        result = await indexing.reembed_workspace(
            session, workspace_id=ws, new_version=2, embedder=DeterministicEmbedder()
        )
    assert result["new_version"] == 2

    async with session_scope(ws) as session:
        versions = (
            (await session.execute(select(ContentChunk.emb_version).distinct())).scalars().all()
        )
        active = await session.scalar(select(KnowledgeSettings.emb_version))
        # Retrieval now serves the new version (get_active_emb_version reads the flipped setting).
        results = await retrieve(session, workspace_id=ws, query="how do I get a refund", k=5)
    assert versions == [2]  # old version cleaned up
    assert active == 2  # atomic cutover flipped the pointer
    assert any(r.source_id == art_id for r in results)
