"""Integration tests for hybrid retrieval + tenant isolation (P1.1, RFC-002 §5.5/§7, App. B)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from relay.core.db import get_sessionmaker, session_scope
from relay.core.ids import IdPrefix, decode_public_id, uuid7
from relay.modules.knowledge.chunking import Chunk
from relay.modules.knowledge.embeddings import DeterministicEmbedder, embed_text
from relay.modules.knowledge.indexing import index_chunks
from relay.modules.knowledge.models import ContentChunk
from relay.modules.knowledge.retrieval import retrieve
from relay.modules.knowledge.vectors import to_vector_literal

pytestmark = pytest.mark.integration

PASSWORD = "correct-horse-battery-staple"


async def _workspace(client, name: str):
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
    return decode_public_id(IdPrefix.WORKSPACE, resp.json()["workspace"]["id"])


async def _ingest(ws, source_id, content: str, *, title: str | None = None) -> None:
    async with session_scope(ws) as session:
        await index_chunks(
            session,
            workspace_id=ws,
            source_kind="article",
            source_id=source_id,
            locale="en",
            audience={},
            title=title,
            chunks=[Chunk(chunk_index=0, content=content, heading_path=None, token_count=10)],
            embedder=DeterministicEmbedder(),
            emb_version=1,
        )


async def test_hybrid_retrieval_finds_relevant_chunk(client) -> None:
    ws = await _workspace(client, "kb-basic")
    refund = uuid7()
    await _ingest(ws, refund, "Refunds are processed within 30 days for any subscription.")
    await _ingest(ws, uuid7(), "Change your shipping address in the delivery settings page.")

    async with session_scope(ws) as session:
        results = await retrieve(
            session, workspace_id=ws, query="how do I get a refund", k=5, emb_version=1
        )
    assert results
    assert results[0].source_id == refund


async def test_cross_tenant_retrieval_is_impossible_adversarial(client) -> None:
    """Workspace B plants a chunk whose vector == A's query (the global nearest neighbour). A must
    still never retrieve it — the SQL layer (RLS) cannot return another tenant's rows."""
    ws_a = await _workspace(client, "kb-a")
    ws_b = await _workspace(client, "kb-b")
    query = "how do I get a refund for my subscription"

    a_source = uuid7()
    await _ingest(ws_a, a_source, "Refunds take up to 30 days to appear on your statement.")
    # Adversarial: content == the exact query and title=None, so its embedding IS the query vector.
    b_source = uuid7()
    await _ingest(ws_b, b_source, query, title=None)

    async with session_scope(ws_a) as session:
        results = await retrieve(session, workspace_id=ws_a, query=query, k=10, emb_version=1)
    assert results
    assert all(r.source_id != b_source for r in results)
    assert all(r.source_id == a_source for r in results)

    # RLS backstop: a nearest-neighbour scan with NO workspace predicate, run in A's session, still
    # returns only A's rows — RLS catches it even with the app-layer filter removed (P0.1 regime).
    async with session_scope(ws_a) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT workspace_id FROM content_chunks "
                    "ORDER BY embedding <=> CAST(:q AS halfvec) LIMIT 20"
                ),
                {"q": to_vector_literal(embed_text(query))},
            )
        ).all()
    assert rows
    assert all(r[0] == ws_a for r in rows)


async def test_unset_guc_returns_zero_chunks(client) -> None:
    ws = await _workspace(client, "kb-guc")
    await _ingest(ws, uuid7(), "Some content that definitely exists in this workspace.")
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        # Deliberately DO NOT set app.ws.
        count = await session.scalar(select(func.count()).select_from(ContentChunk))
    assert count == 0


async def test_source_kind_filter_and_methods(client) -> None:
    ws = await _workspace(client, "kb-filter")
    art = uuid7()
    await _ingest(ws, art, "Password reset instructions for your account login.")
    async with session_scope(ws) as session:
        for method in ("hybrid", "vector", "fts"):
            res = await retrieve(
                session, workspace_id=ws, query="password reset login", method=method, emb_version=1
            )
            assert any(r.source_id == art for r in res), method
        # Filtering to a kind with no chunks yields nothing.
        empty = await retrieve(
            session,
            workspace_id=ws,
            query="password reset login",
            source_kinds=["pdf"],
            emb_version=1,
        )
    assert empty == []
