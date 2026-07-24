"""Unit tests for the predicate AST (core/predicates.py) — the shared filter/condition language."""

from __future__ import annotations

import pytest

from relay.core.errors import ValidationError
from relay.core.predicates import evaluate, validate_predicate

CTX = {
    "channel": "email",
    "state": "open",
    "count": 5,
    "tags": ["vip", "urgent"],
    "nested": {"score": 0.9},
    "flag": True,
    "empty": None,
}


@pytest.mark.parametrize(
    ("pred", "expected"),
    [
        ({"op": "eq", "field": "channel", "value": "email"}, True),
        ({"op": "eq", "field": "channel", "value": "chat"}, False),
        ({"op": "ne", "field": "channel", "value": "chat"}, True),
        ({"op": "ne", "field": "missing", "value": "x"}, True),  # missing != anything
        ({"op": "eq", "field": "missing", "value": "x"}, False),  # missing never == a value
        ({"op": "gt", "field": "count", "value": 3}, True),
        ({"op": "gte", "field": "count", "value": 5}, True),
        ({"op": "lt", "field": "count", "value": 3}, False),
        ({"op": "lte", "field": "count", "value": 5}, True),
        ({"op": "gt", "field": "channel", "value": 3}, False),  # str vs int → not comparable
        ({"op": "gt", "field": "missing", "value": 3}, False),
        ({"op": "in", "field": "state", "value": ["open", "snoozed"]}, True),
        ({"op": "in", "field": "state", "value": ["closed"]}, False),
        ({"op": "contains", "field": "tags", "value": "vip"}, True),
        ({"op": "contains", "field": "tags", "value": "nope"}, False),
        ({"op": "contains", "field": "channel", "value": "mail"}, True),  # substring
        ({"op": "exists", "field": "channel"}, True),
        ({"op": "exists", "field": "empty"}, False),  # None → not exists
        ({"op": "exists", "field": "missing"}, False),
        ({"op": "not_exists", "field": "missing"}, True),
        ({"op": "not_exists", "field": "channel"}, False),
        ({"op": "eq", "field": "nested.score", "value": 0.9}, True),  # dotted path
        ({"op": "eq", "field": "nested.missing", "value": 1}, False),
        ({"op": "eq", "field": "flag", "value": True}, True),
    ],
)
def test_evaluate_leaf_ops(pred: dict, expected: bool) -> None:
    assert evaluate(pred, CTX) is expected


def test_evaluate_logical() -> None:
    assert evaluate({"op": "and", "clauses": []}, CTX) is True  # vacuous truth
    assert evaluate({"op": "or", "clauses": []}, CTX) is False
    both = {
        "op": "and",
        "clauses": [
            {"op": "eq", "field": "channel", "value": "email"},
            {"op": "gt", "field": "count", "value": 1},
        ],
    }
    assert evaluate(both, CTX) is True
    either = {
        "op": "or",
        "clauses": [
            {"op": "eq", "field": "channel", "value": "chat"},
            {"op": "eq", "field": "state", "value": "open"},
        ],
    }
    assert evaluate(either, CTX) is True
    assert evaluate({"op": "not", "clause": {"op": "eq", "field": "state", "value": "closed"}}, CTX)


def test_evaluate_is_total_on_malformed() -> None:
    # A malformed node (bypassing validation) must never raise — it evaluates falsey.
    assert evaluate({"op": "bogus", "field": "x"}, CTX) is False
    assert evaluate({"op": "not", "clause": "notadict"}, CTX) is False


@pytest.mark.parametrize(
    "pred",
    [
        {"op": "and", "clauses": [{"op": "eq", "field": "a", "value": 1}]},
        {"op": "not", "clause": {"op": "exists", "field": "a"}},
        {"op": "eq", "field": "a", "value": 1},
        {"op": "exists", "field": "a"},
        {"op": "in", "field": "a", "value": [1, 2]},
    ],
)
def test_validate_accepts_valid(pred: dict) -> None:
    validate_predicate(pred)  # must not raise


@pytest.mark.parametrize(
    "pred",
    [
        "notadict",
        {"op": "bogus", "field": "a", "value": 1},
        {"op": "and"},  # missing clauses
        {"op": "and", "clauses": "x"},
        {"op": "not"},  # missing clause
        {"op": "eq", "value": 1},  # missing field
        {"op": "eq", "field": "", "value": 1},  # empty field
        {"op": "eq", "field": "a"},  # missing value
        {"op": "in", "field": "a", "value": "notalist"},  # in needs a list
    ],
)
def test_validate_rejects_invalid(pred: object) -> None:
    with pytest.raises(ValidationError):
        validate_predicate(pred)


def test_validate_depth_guard() -> None:
    node: dict = {"op": "exists", "field": "a"}
    for _ in range(40):
        node = {"op": "not", "clause": node}
    with pytest.raises(ValidationError):
        validate_predicate(node)
