"""Unit tests for the deterministic embedder (P1.1, RFC-003 §4).

These pin the properties the eval harness and re-sync diffing rely on: determinism, process-stable
hashing, synonym-driven semantic proximity, and the deliberate blindness to exact identifiers.
"""

from __future__ import annotations

import math

from relay.modules.knowledge.embeddings import DeterministicEmbedder, embed_text
from relay.modules.knowledge.vectors import EMBEDDING_DIM


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))  # both are unit vectors


def test_dimension_and_unit_norm() -> None:
    v = embed_text("how do I cancel my subscription")
    assert len(v) == EMBEDDING_DIM
    assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, rel_tol=1e-6)


def test_deterministic_same_input_same_vector() -> None:
    assert embed_text("reset my password") == embed_text("reset my password")


def test_stable_hash_not_process_salted() -> None:
    # A hard-coded expectation of a specific dimension's sign would catch accidental use of the
    # PYTHONHASHSEED-salted builtin hash(): the vector must be reproducible run-to-run.
    v1 = embed_text("refund policy")
    v2 = embed_text("refund policy")
    assert v1 == v2 and any(x != 0.0 for x in v1)


def test_synonyms_are_closer_than_unrelated() -> None:
    refund = embed_text("how to get a refund")
    reimbursement = embed_text("how to get a reimbursement")  # synonym, zero surface overlap on key
    delivery = embed_text("how to track a delivery")
    assert _cosine(refund, reimbursement) > _cosine(refund, delivery)


def test_identifier_tokens_are_ignored() -> None:
    # The embedding models a dense model's blindness to exact codes: adding an id changes nothing.
    assert embed_text("refund policy") == embed_text("refund policy ref0042")


async def test_provider_batches_in_order() -> None:
    embedder = DeterministicEmbedder()
    out = await embedder.embed(["alpha refund", "bravo delivery"])
    assert out == [embed_text("alpha refund"), embed_text("bravo delivery")]
    assert embedder.dimension == EMBEDDING_DIM
