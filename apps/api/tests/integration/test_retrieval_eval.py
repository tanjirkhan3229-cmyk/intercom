"""Retrieval eval gate (P1.1 acceptance): recall@10 >= 0.85 and hybrid beats both baselines.

Ingests the three synthetic corpora into three real workspaces and scores hybrid / vector-only /
FTS-only retrieval, persisting ``retrieval_evals`` rows. This is the CI regression gate for
retrieval quality (RFC-003 §8) — a prompt/model/retrieval change that regresses recall fails here.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import func, select

from relay.core.db import session_scope
from relay.core.ids import IdPrefix, decode_public_id
from relay.modules.knowledge.eval_corpora import build_corpora
from relay.modules.knowledge.eval_harness import evaluate_and_store
from relay.modules.knowledge.models import RetrievalEval

pytestmark = pytest.mark.integration

PASSWORD = "correct-horse-battery-staple"
RECALL_FLOOR = 0.85


async def _workspace(client, name: str) -> str:
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
    return resp.json()["workspace"]["id"]


async def test_retrieval_eval_gate(client) -> None:
    corpora = build_corpora()
    summary: list[str] = []
    for corpus in corpora:
        assert len(corpus.docs) >= 200
        ws = decode_public_id(IdPrefix.WORKSPACE, await _workspace(client, f"kb-{corpus.name}"))
        async with session_scope(ws) as session:
            results = await evaluate_and_store(
                session, workspace_id=ws, corpus=corpus, k=10, ef_search=200, emb_version=1
            )

        hybrid, vector, fts = results["hybrid"], results["vector"], results["fts"]
        summary.append(
            f"{corpus.name:9s} hybrid r@10={hybrid.recall_at_k:.3f} mrr={hybrid.mrr:.3f} | "
            f"vector r@10={vector.recall_at_k:.3f} | fts r@10={fts.recall_at_k:.3f} "
            f"(n={hybrid.num_queries})"
        )

        assert hybrid.recall_at_k >= RECALL_FLOOR, f"{corpus.name}: {hybrid.recall_at_k}"
        assert hybrid.recall_at_k > vector.recall_at_k, f"{corpus.name}: hybrid !> vector"
        assert hybrid.recall_at_k > fts.recall_at_k, f"{corpus.name}: hybrid !> fts"

        # Runs were persisted (one row per method).
        async with session_scope(ws) as session:
            count = await session.scalar(
                select(func.count())
                .select_from(RetrievalEval)
                .where(RetrievalEval.corpus == corpus.name)
            )
        assert count == 3

    print("\n" + "\n".join(summary))
