# Retrieval eval harness (P1.1)

Retrieval quality is where Neko's resolution rate lives (RFC-003 §4), so it is gated in CI like any
other correctness property. The harness ingests labelled synthetic corpora, runs the three retrieval
methods, and scores **recall@k** and **MRR** at the document level — a prompt/model/retrieval change
that regresses recall fails the build.

- Harness: `apps/api/src/relay/modules/knowledge/eval_harness.py`
- Corpora: `apps/api/src/relay/modules/knowledge/eval_corpora.py`
- CI gate (integration): `apps/api/tests/integration/test_retrieval_eval.py`
- Results ledger: `retrieval_evals` table (one row per corpus × method)

Run it locally:

```bash
cd apps/api && uv run pytest tests/integration/test_retrieval_eval.py -s
```

## What it measures

Three retrieval methods over the RFC-002 Appendix B query shape:

- **hybrid** — pgvector HNSW (ANN over `halfvec`) + Postgres FTS, fused with reciprocal-rank fusion
  (`1/(60+rank)`).
- **vector** — the ANN arm alone.
- **fts** — `websearch_to_tsquery('simple', …)` alone.

`recall@10` = fraction of labelled queries whose gold document appears in the top-10 results;
`MRR` = mean reciprocal rank of the first gold hit.

## The corpora are an honest complementarity benchmark

Three "workspaces" (`billing`, `shipping`, `devapi`) of **≥200 docs each**, generated deterministically
(no RNG) so runs are reproducible. Docs live in large, uniform clusters that share an
`(area, action, filler)` vocabulary; within a cluster a doc is distinguished only by a globally-unique
`(adjective, noun)` feature pair and a unique `ref####` code. Two query families exercise the two
failure modes that make hybrid worth building:

| Family | Construction | Vector arm | FTS arm |
|---|---|---|---|
| `exact` | cluster words + the unique **code** (no pair) | disperses across the cluster (the embedder ignores identifier tokens, modelling a dense model's blindness to exact codes) → **misses ~half** | pins the code by exact AND-match → **hits** |
| `paraphrase` | thesaurus **synonyms** of action/area + the unique pair | matches via synonym-group features + the pair → **hits** | the synonym tokens are absent from the doc → AND-match fails → **misses** |

Neither arm is complete; RRF fusion recovers both. This mirrors why real hybrid retrieval exists:
dense embeddings generalise to paraphrases but fumble exact identifiers, and lexical FTS is the
opposite. (The dev/CI embedder is deterministic and hermetic — see `embeddings.py`. In production the
provider abstraction swaps in a real embedding model behind the same interface; the corpora exercise
the *retrieval* machinery, not a specific model's semantics.)

## Measured numbers (deterministic embedder, `ef_search=200`, k=10)

| corpus | hybrid recall@10 | hybrid MRR | vector-only recall@10 | fts-only recall@10 |
|---|---|---|---|---|
| billing | **1.000** | 0.765 | 0.709 | 0.500 |
| shipping | **1.000** | 0.769 | 0.739 | 0.500 |
| devapi | **1.000** | 0.754 | 0.746 | 0.500 |

(n = 134 labelled queries per corpus.)

## Gate criteria (P1.1 acceptance)

For every corpus the CI test asserts:

1. `hybrid.recall@10 ≥ 0.85` — the retrieval quality floor.
2. `hybrid.recall@10 > vector.recall@10` — fusion beats the vector arm (FTS rescues exact-code misses).
3. `hybrid.recall@10 > fts.recall@10` — fusion beats the FTS arm (vector rescues paraphrase misses).
4. Every run is persisted to `retrieval_evals` (regression history).

Because the corpora and the deterministic embedder are fixed, these numbers are stable run-to-run; a
change to chunking, the RRF fusion, the query shape, or the embedder that moves them is caught here.
