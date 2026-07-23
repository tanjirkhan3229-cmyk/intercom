"""Retrieval eval harness (P1.1) — the CI regression gate for retrieval quality (RFC-003 §8).

Ingests a labelled :class:`~relay.modules.knowledge.eval_corpora.EvalCorpus` into a workspace's
``content_chunks`` (each doc = one chunk), then scores the three retrieval methods (hybrid /
vector-only / FTS-only) with **recall@k** and **MRR** at the document level, and persists a
``retrieval_evals`` row per method. The gate (see tests + ``scripts``) asserts hybrid recall@10
clears the floor and beats both single-signal baselines.

Everything runs under the caller's ``session_scope(workspace_id)`` (RLS-scoped), so the harness is
also a live proof that retrieval never crosses tenants.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from relay.core.ids import uuid7
from relay.modules.knowledge.chunking import estimate_tokens
from relay.modules.knowledge.embeddings import EmbeddingProvider, get_embedder
from relay.modules.knowledge.eval_corpora import EvalCorpus
from relay.modules.knowledge.indexing import _content_hash
from relay.modules.knowledge.retrieval import RetrievalMethod, retrieve
from relay.modules.knowledge.vectors import to_vector_literal

_BULK_INSERT_SQL = text(
    """
    INSERT INTO content_chunks
      (id, workspace_id, source_kind, source_id, locale, audience, title, heading_path,
       chunk_index, content, content_hash, token_count, embedding, emb_version,
       created_at, updated_at)
    VALUES
      (:id, :ws, 'article', :sid, 'en', '{}'::jsonb, :title, NULL,
       0, :content, :chash, :tokens, CAST(:emb AS halfvec), :ver, now(), now())
    """
)


@dataclass(frozen=True)
class MethodResult:
    method: str
    recall_at_k: float
    mrr: float
    num_queries: int


async def ingest_corpus(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    corpus: EvalCorpus,
    embedder: EmbeddingProvider,
    emb_version: int,
) -> int:
    """Embed + insert every doc as a single-chunk ``article`` source. Returns rows written."""
    contents = [f"{d.title}. {d.text}" for d in corpus.docs]
    vectors = await embedder.embed(contents)
    params = [
        {
            "id": uuid7(),
            "ws": workspace_id,
            "sid": doc.doc_id,
            "title": doc.title,
            "content": content,
            "chash": _content_hash(content),
            "tokens": estimate_tokens(content),
            "emb": to_vector_literal(vector),
            "ver": emb_version,
        }
        for doc, content, vector in zip(corpus.docs, contents, vectors, strict=True)
    ]
    await session.execute(_BULK_INSERT_SQL, params)
    return len(params)


def _recall_and_rr(source_order: list[uuid.UUID], gold: uuid.UUID, k: int) -> tuple[int, float]:
    """recall@k hit + reciprocal rank at doc level (dedups source ids, order-preserving)."""
    seen: list[uuid.UUID] = []
    for sid in source_order:
        if sid not in seen:
            seen.append(sid)
    hit = 1 if gold in seen[:k] else 0
    rr = 0.0
    for rank, sid in enumerate(seen, start=1):
        if sid == gold:
            rr = 1.0 / rank
            break
    return hit, rr


async def score_method(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    corpus: EvalCorpus,
    method: RetrievalMethod,
    k: int,
    ef_search: int,
    embedder: EmbeddingProvider,
    emb_version: int,
) -> MethodResult:
    """Run every labelled query under ``method`` and average recall@k + MRR."""
    hits = 0
    rr_sum = 0.0
    for q in corpus.queries:
        results = await retrieve(
            session,
            workspace_id=workspace_id,
            query=q.query,
            k=k,
            ef_search=ef_search,
            method=method,
            emb_version=emb_version,
            embedder=embedder,
        )
        hit, rr = _recall_and_rr([r.source_id for r in results], q.gold_doc_id, k)
        hits += hit
        rr_sum += rr
    n = len(corpus.queries)
    return MethodResult(
        method=method,
        recall_at_k=hits / n if n else 0.0,
        mrr=rr_sum / n if n else 0.0,
        num_queries=n,
    )


async def evaluate_and_store(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    corpus: EvalCorpus,
    k: int = 10,
    ef_search: int = 200,
    embedder: EmbeddingProvider | None = None,
    emb_version: int = 1,
    store: bool = True,
) -> dict[str, MethodResult]:
    """Ingest ``corpus`` (if not already), score all three methods, persist ``retrieval_evals``."""
    embedder = embedder or get_embedder()
    existing = await session.scalar(
        text("SELECT count(*) FROM content_chunks WHERE workspace_id = :ws"),
        {"ws": workspace_id},
    )
    if not existing:
        await ingest_corpus(
            session,
            workspace_id=workspace_id,
            corpus=corpus,
            embedder=embedder,
            emb_version=emb_version,
        )

    results: dict[str, MethodResult] = {}
    for method in ("hybrid", "vector", "fts"):
        res = await score_method(
            session,
            workspace_id=workspace_id,
            corpus=corpus,
            method=method,
            k=k,
            ef_search=ef_search,
            embedder=embedder,
            emb_version=emb_version,
        )
        results[method] = res
        if store:
            await session.execute(
                text(
                    """
                    INSERT INTO retrieval_evals
                      (id, workspace_id, created_at, corpus, method, k, recall_at_k, mrr,
                       num_queries, emb_version, params)
                    VALUES
                      (:id, :ws, now(), :corpus, :method, :k, :recall, :mrr, :n, :ver,
                       CAST(:params AS jsonb))
                    """
                ),
                {
                    "id": uuid7(),
                    "ws": workspace_id,
                    "corpus": corpus.name,
                    "method": method,
                    "k": k,
                    "recall": res.recall_at_k,
                    "mrr": res.mrr,
                    "n": res.num_queries,
                    "ver": emb_version,
                    "params": f'{{"ef_search": {ef_search}}}',
                },
            )
    return results
