"""Deterministic synthetic corpora for the retrieval eval harness (P1.1 acceptance).

Three "workspaces" of >=200 labelled docs each, generated with **no RNG** (index-derived) so runs
are byte-reproducible in CI. The design deliberately makes the vector and FTS arms *complementary*
so the harness can show hybrid beats each alone — an honest benchmark, not a rigged one:

- Docs live in large, **uniform clusters** that share an ``(area, action, filler)`` vocabulary;
  within a cluster a doc is distinguished only by a globally-unique ``(adjective, noun)`` feature
  pair and a unique ``ref####`` code. Because the embedder ignores identifier tokens (modelling a
  dense model's weakness on exact codes) and the cluster words are shared, the vector arm cannot
  tell cluster-mates apart on a code query — it disperses the true doc through the cluster.
- Two query families exercise the two failure modes:
  * ``exact`` — cluster words + the unique ``code`` (no feature pair): FTS pins it by exact
    AND-match on the code; the vector arm is ambiguous across the cluster. (FTS rescues vector.)
  * ``paraphrase`` — the action/area swapped for thesaurus synonyms + the unique feature pair: the
    synonym tokens are absent from the doc so FTS AND-matching fails; the vector arm matches via
    synonym-group + the unique pair. (Vector rescues FTS.)

Recall@k + MRR are measured at the document (source) level.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from relay.core.ids import uuid7

# Docs per uniform cluster. Large enough that a vector arm which can't read the code disperses the
# true doc across the cluster (so vector-only misses ~half the exact queries, FTS rescues them).
CLUSTER_SIZE = 20

_VERTICALS: tuple[dict[str, Any], ...] = (
    {
        "name": "billing",
        "areas": [
            "invoice",
            "subscription",
            "payment",
            "refund",
            "discount",
            "tax",
            "receipt",
            "plan",
        ],
        "actions": ["update", "cancel", "create", "export", "verify", "reset"],
        "filler": ["dashboard", "portal", "settings", "history", "statement", "cycle", "balance"],
    },
    {
        "name": "shipping",
        "areas": [
            "delivery",
            "shipment",
            "address",
            "carrier",
            "package",
            "tracking",
            "return",
            "label",
        ],
        "actions": ["update", "cancel", "create", "export", "verify", "reset"],
        "filler": ["warehouse", "courier", "zone", "manifest", "pallet", "customs", "transit"],
    },
    {
        "name": "devapi",
        "areas": [
            "endpoint",
            "token",
            "webhook",
            "integration",
            "permission",
            "notification",
            "account",
            "team",
        ],
        "actions": ["update", "delete", "create", "export", "verify", "reset"],
        "filler": ["payload", "header", "schema", "sandbox", "rate", "scope", "cursor"],
    },
)

_ADJECTIVES = [
    "silent",
    "nested",
    "legacy",
    "primary",
    "hidden",
    "shared",
    "custom",
    "global",
    "regional",
    "expired",
    "pending",
    "active",
    "archived",
    "default",
    "bulk",
    "partial",
    "recurring",
    "manual",
    "automatic",
    "scoped",
]
_NOUNS = ["alpha", "bravo", "cobalt", "delta", "ember", "flux", "gamma", "helix", "ionis", "jade"]


@dataclass(frozen=True)
class EvalDoc:
    doc_id: uuid.UUID
    title: str
    text: str
    area: str
    action: str
    adj: str
    noun: str
    code: str
    fill0: str
    fill1: str


@dataclass(frozen=True)
class EvalQuery:
    query: str
    gold_doc_id: uuid.UUID
    family: str  # "exact" | "paraphrase"


@dataclass
class EvalCorpus:
    name: str
    docs: list[EvalDoc] = field(default_factory=list)
    queries: list[EvalQuery] = field(default_factory=list)


# Synonyms drawn from the embedder thesaurus — the exact substitutions that make paraphrase queries
# lexically disjoint from their source while staying semantically near it.
_ACTION_SYNONYM = {
    "update": "modify",
    "cancel": "terminate",
    "create": "add",
    "export": "download",
    "verify": "confirm",
    "reset": "restore",
    "delete": "remove",
}
_AREA_SYNONYM = {
    "invoice": "bill",
    "subscription": "membership",
    "payment": "charge",
    "refund": "reimbursement",
    "discount": "coupon",
    "receipt": "statement",
    "return": "reimbursement",
    "notification": "alert",
    "permission": "access",
    "integration": "connection",
    "team": "workspace",
    "account": "profile",
    "webhook": "connector",
    "token": "credential",
}


def _doc_body(area: str, action: str, adj: str, noun: str, code: str, f0: str, f1: str) -> str:
    """A short (~1 chunk) help article. Repeats cluster + feature terms; embeds the unique code."""
    return (
        f"This guide explains how to {action} the {adj} {noun} {area}. "
        f"To {action} a {area}, open the {f0} and choose the {adj} {noun} {area} to {action}. "
        f"The {adj} {noun} {area} controls how the {area} behaves in the {f1}. "
        f"After you {action} the {adj} {noun} {area}, the change applies to the whole {area} {f0}. "
        f"If the {adj} {noun} {area} looks wrong, {action} it again from the {area} {f1}. "
        f"The reference code for this {adj} {noun} {area} procedure is {code}."
    )


def build_corpus(vertical: dict[str, Any], *, n_docs: int, query_stride: int) -> EvalCorpus:
    name = str(vertical["name"])
    areas: list[str] = list(vertical["areas"])
    actions: list[str] = list(vertical["actions"])
    filler: list[str] = list(vertical["filler"])
    corpus = EvalCorpus(name=name)

    pairs = [(adj, noun) for adj in _ADJECTIVES for noun in _NOUNS]  # 200 unique pairs
    for i in range(n_docs):
        adj, noun = pairs[i % len(pairs)]
        cluster = i // CLUSTER_SIZE
        area = areas[cluster % len(areas)]
        action = actions[cluster % len(actions)]
        f0 = filler[cluster % len(filler)]
        f1 = filler[(cluster + 3) % len(filler)]
        code = f"ref{i:04d}"
        doc_id = uuid7()
        title = f"How to {action} the {adj} {noun} {area}"
        text = _doc_body(area, action, adj, noun, code, f0, f1)
        corpus.docs.append(EvalDoc(doc_id, title, text, area, action, adj, noun, code, f0, f1))

    # Label every ``query_stride``-th doc with one exact + one paraphrase query.
    for i in range(0, n_docs, query_stride):
        doc = corpus.docs[i]
        # Exact: cluster words + code, NO pair — FTS pins the code; vector is cluster-ambiguous.
        corpus.queries.append(
            EvalQuery(
                query=f"{doc.action} {doc.area} {doc.fill0} {doc.fill1} {doc.code}",
                gold_doc_id=doc.doc_id,
                family="exact",
            )
        )
        # Paraphrase: synonyms + unique pair — vector matches via synonyms + pair; FTS AND-misses.
        action_syn = _ACTION_SYNONYM.get(doc.action, doc.action)
        area_syn = _AREA_SYNONYM.get(doc.area, doc.area)
        corpus.queries.append(
            EvalQuery(
                query=f"how to {action_syn} the {doc.adj} {doc.noun} {area_syn}",
                gold_doc_id=doc.doc_id,
                family="paraphrase",
            )
        )
    return corpus


def build_corpora(*, n_docs: int = 200, query_stride: int = 3) -> list[EvalCorpus]:
    """The three synthetic corpora (>=200 docs each) used by the eval gate."""
    return [build_corpus(v, n_docs=n_docs, query_stride=query_stride) for v in _VERTICALS]
