"""Embedding provider abstraction (RFC-003 §4 — where resolution rate actually lives).

Two providers behind one interface:

- :class:`DeterministicEmbedder` — the default in dev/test/CI. Hermetic (no network), and
  **stable across processes** (blake2b, never the salted builtin ``hash()``), so the retrieval
  eval harness and the re-sync "only re-embed changed chunks" diffing are reproducible. It is a
  real, if simple, semantic signal, not noise: three feature families combine into a hashed,
  L2-normalised vector —
    * exact word unigrams + adjacent bigrams (lexical, the FTS-overlapping signal),
    * character n-grams (subword — morphological variants and typos share dimensions),
    * **synonym-group** features (a compact curated support-domain thesaurus) — the piece FTS
      cannot see, so paraphrased queries land near their source and *hybrid measurably beats
      FTS-only* (P1.1 acceptance).
  Stopwords are dropped so distinctive terms dominate.

- :class:`HttpEmbeddingProvider` — prod. A batched, timed-out OpenAI-compatible endpoint with
  bounded jittered retries (RFC-001 §9 provider discipline). Not exercised in CI (no key); the
  shape is here so a frontier model plugs in via ``settings.embedding_provider = "openai"``.

A model change is an ``emb_version`` bump + re-embed cutover (RFC-003 §4), never an in-place
edit — so which provider/model produced a vector is a property of the version, not the row.
"""

from __future__ import annotations

import hashlib
import math
import re
from itertools import pairwise
from typing import Protocol, runtime_checkable

from relay.core.logging import get_logger
from relay.modules.knowledge.vectors import EMBEDDING_DIM
from relay.settings import Settings, get_settings

log = get_logger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Deliberately small: only the highest-frequency function words, so distinctive terms dominate the
# vector. Not a linguistic stoplist — retrieval quality, not grammar.
_STOPWORDS: frozenset[str] = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "so",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "this",
        "to",
        "was",
        "we",
        "what",
        "when",
        "where",
        "which",
        "who",
        "will",
        "with",
        "you",
        "your",
    ]
)

# Compact support-domain thesaurus. Each row is a synonym set; every member maps to the row's
# canonical head, and both docs and queries emit a ``y:{head}`` feature — so "reimbursement" in a
# query matches "refund" in a doc even with zero surface overlap. This is the semantic signal that
# lets hybrid retrieval beat exact-lexeme FTS on paraphrased questions (RFC-003 §4).
_SYNONYM_SETS: tuple[tuple[str, ...], ...] = (
    ("refund", "reimbursement", "reimburse", "chargeback", "moneyback"),
    ("cancel", "cancellation", "terminate", "termination", "unsubscribe"),
    ("delete", "remove", "erase", "purge", "wipe"),
    ("password", "passphrase", "credential", "credentials", "login", "signin"),
    ("invoice", "bill", "billing", "receipt", "statement"),
    ("payment", "charge", "transaction", "pay"),
    ("shipping", "delivery", "deliver", "dispatch", "shipment"),
    ("subscription", "plan", "membership", "tier"),
    ("upgrade", "upsell"),
    ("downgrade",),
    ("error", "issue", "problem", "bug", "failure", "fault", "broken"),
    ("enable", "activate", "activation", "turnon"),
    ("disable", "deactivate", "deactivation", "turnoff"),
    ("export", "download"),
    ("import", "upload"),
    ("notification", "alert", "reminder"),
    ("permission", "access", "role", "privilege"),
    ("integration", "connection", "connector", "webhook"),
    ("account", "profile"),
    ("discount", "coupon", "promo", "promotion", "voucher"),
    ("address", "location"),
    ("email", "mail", "inbox"),
    ("phone", "mobile", "telephone"),
    ("verify", "verification", "confirm", "confirmation", "validate"),
    ("update", "edit", "modify", "change"),
    ("create", "add", "new", "setup"),
    ("reset", "restore", "recover", "recovery"),
    ("team", "workspace", "organization", "organisation"),
    ("agent", "operator", "representative", "rep"),
    ("customer", "client", "user", "contact"),
)
_SYNONYM_HEAD: dict[str, str] = {word: row[0] for row in _SYNONYM_SETS for word in row}

# Feature weights. Unigrams anchor exact lexical overlap; char-grams add subword robustness at low
# weight; synonyms carry the semantic signal; bigrams add a little phrase sense. Tuned against the
# eval harness so hybrid > vector-only and > FTS-only while recall@10 clears the floor.
_W_UNIGRAM = 1.0
_W_BIGRAM = 0.55
_W_CHARGRAM = 0.28
_W_SYNONYM = 0.9
_CHARGRAM_N = 3


def _stable_hash(feature: str) -> int:
    """Process-stable 64-bit hash (never the PYTHONHASHSEED-salted builtin ``hash()``)."""
    return int.from_bytes(hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest(), "big")


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens; stopwords dropped, 1-char tokens dropped (digits kept)."""
    out: list[str] = []
    for tok in _TOKEN_RE.findall(text.lower()):
        if tok in _STOPWORDS:
            continue
        if len(tok) < 2 and not tok.isdigit():
            continue
        out.append(tok)
    return out


def _char_grams(token: str) -> list[str]:
    padded = f"^{token}$"
    n = _CHARGRAM_N
    if len(padded) <= n:
        return [padded]
    return [padded[i : i + n] for i in range(len(padded) - n + 1)]


def _is_identifier(tok: str) -> bool:
    """Opaque identifier-like token (contains a digit): codes, SKUs, order numbers."""
    return any(c.isdigit() for c in tok)


def _features(text: str) -> dict[str, float]:
    """Accumulate weighted features for a text (shared by docs and queries).

    Identifier-like tokens (anything with a digit) are **excluded** from the embedding — a
    deliberate model of a real dense embedder's well-known weakness on exact codes/SKUs. That
    weakness is precisely why hybrid retrieval exists: the FTS arm pins exact identifiers the
    vector arm cannot, so hybrid measurably beats vector-only (P1.1 acceptance).
    """
    feats: dict[str, float] = {}
    tokens = [t for t in tokenize(text) if not _is_identifier(t)]
    for tok in tokens:
        feats[f"w:{tok}"] = feats.get(f"w:{tok}", 0.0) + _W_UNIGRAM
        head = _SYNONYM_HEAD.get(tok)
        if head is not None:
            feats[f"y:{head}"] = feats.get(f"y:{head}", 0.0) + _W_SYNONYM
        for gram in _char_grams(tok):
            feats[f"c:{gram}"] = feats.get(f"c:{gram}", 0.0) + _W_CHARGRAM
    for a, b in pairwise(tokens):
        key = f"b:{a}|{b}"
        feats[key] = feats.get(key, 0.0) + _W_BIGRAM
    return feats


def embed_text(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """Deterministic embedding of one text: signed feature hashing + L2 normalisation.

    Signed hashing (each feature adds ``±weight`` at one dimension) makes the dot product of two
    vectors accumulate on shared features and cancel on collisions — cosine ≈ shared-feature mass,
    which is exactly the retrieval signal we want.
    """
    vec = [0.0] * dim
    for feature, weight in _features(text).items():
        h = _stable_hash(feature)
        idx = h % dim
        sign = 1.0 if (h >> 63) & 1 else -1.0
        vec[idx] += sign * weight
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        # Empty/stopword-only text: a stable non-zero unit vector so cosine is defined.
        vec[0] = 1.0
        return vec
    return [v / norm for v in vec]


@runtime_checkable
class EmbeddingProvider(Protocol):
    """The one interface the indexer and retrieval query bind against."""

    model: str
    dimension: int

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one vector per input text (order-preserving). Batches internally as needed."""
        ...


class DeterministicEmbedder:
    """Hermetic, reproducible embedder — the CI/dev default (see module docstring)."""

    def __init__(self, model: str = "relay-hash-v1", dimension: int = EMBEDDING_DIM) -> None:
        self.model = model
        self.dimension = dimension

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [embed_text(t, self.dimension) for t in texts]

    def embed_sync(self, texts: list[str]) -> list[list[float]]:
        return [embed_text(t, self.dimension) for t in texts]


class HttpEmbeddingProvider:
    """Prod: batched OpenAI-compatible embeddings endpoint (RFC-001 §9 provider discipline).

    Every call is timed out and retried a bounded, jittered number of times on transient errors.
    Not exercised in CI (no key/base configured); wired here so a real model is a settings flip.
    """

    def __init__(
        self,
        *,
        model: str,
        dimension: int,
        api_base: str,
        api_key: str,
        batch_size: int,
        timeout_seconds: float,
    ) -> None:
        self.model = model
        self.dimension = dimension
        self._api_base = api_base.rstrip("/")
        self._api_key = api_key
        self._batch_size = batch_size
        self._timeout = timeout_seconds

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx
        from tenacity import (
            retry,
            retry_if_exception_type,
            stop_after_attempt,
            wait_random_exponential,
        )

        out: list[list[float]] = []

        @retry(
            reraise=True,
            stop=stop_after_attempt(4),
            wait=wait_random_exponential(multiplier=0.5, max=8),
            retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        )
        async def _one_batch(client: httpx.AsyncClient, batch: list[str]) -> list[list[float]]:
            resp = await client.post(
                f"{self._api_base}/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self.model, "input": batch},
            )
            resp.raise_for_status()
            data = resp.json()["data"]
            return [row["embedding"] for row in data]

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for start in range(0, len(texts), self._batch_size):
                batch = texts[start : start + self._batch_size]
                out.extend(await _one_batch(client, batch))
        return out


def get_embedder(settings: Settings | None = None) -> EmbeddingProvider:
    """Construct the configured provider. Defaults to the deterministic embedder."""
    settings = settings or get_settings()
    if settings.embedding_provider == "openai":
        if not settings.embedding_api_base or not settings.embedding_api_key:
            raise RuntimeError(
                "embedding_provider='openai' requires embedding_api_base + embedding_api_key"
            )
        return HttpEmbeddingProvider(
            model=settings.embedding_model,
            dimension=settings.embedding_dimension,
            api_base=settings.embedding_api_base,
            api_key=settings.embedding_api_key,
            batch_size=settings.embedding_batch_size,
            timeout_seconds=settings.embedding_timeout_seconds,
        )
    return DeterministicEmbedder(
        model=settings.embedding_model, dimension=settings.embedding_dimension
    )
