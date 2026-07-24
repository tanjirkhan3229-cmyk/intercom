"""Unit tests for the contact audience predicate→SQL compiler (no DB).

Covers the validation/injection surface: unknown fields, unknown/mistyped custom attributes, and
op/type combinations that must be rejected at compile time so a broken segment never reaches a fire.
"""

from __future__ import annotations

import pytest
from sqlalchemy import ColumnElement

from relay.core.errors import ValidationError
from relay.modules.crm.audience import compile_contact_where

_ATTRS = {"plan": "string", "seats": "number", "vip": "boolean", "tags": "list"}


def test_empty_predicate_matches_all() -> None:
    where = compile_contact_where({}, _ATTRS)
    assert isinstance(where, ColumnElement)
    assert compile_contact_where(None, _ATTRS) is not None


@pytest.mark.parametrize(
    "predicate",
    [
        {"op": "eq", "field": "email", "value": "a@b.com"},
        {"op": "ne", "field": "kind", "value": "lead"},
        {"op": "in", "field": "kind", "value": ["user", "lead"]},
        {"op": "contains", "field": "name", "value": "ac"},
        {"op": "exists", "field": "phone"},
        {"op": "eq", "field": "custom.plan", "value": "pro"},
        {"op": "gt", "field": "custom.seats", "value": 5},
        {"op": "eq", "field": "custom.vip", "value": True},
        {"op": "contains", "field": "custom.tags", "value": "beta"},
        {
            "op": "and",
            "clauses": [
                {"op": "eq", "field": "custom.plan", "value": "pro"},
                {"op": "not", "clause": {"op": "exists", "field": "custom.vip"}},
            ],
        },
    ],
)
def test_valid_predicates_compile(predicate: dict[str, object]) -> None:
    assert isinstance(compile_contact_where(predicate, _ATTRS), ColumnElement)


@pytest.mark.parametrize(
    "predicate",
    [
        {"op": "eq", "field": "not_a_field", "value": 1},  # unknown core field
        {"op": "eq", "field": "custom.unknown", "value": 1},  # undefined attribute
        {"op": "eq", "field": "custom.seats", "value": "not-a-number"},  # type mismatch
        {"op": "gt", "field": "custom.tags", "value": "x"},  # ordered op on a list
        {"op": "in", "field": "custom.tags", "value": ["x"]},  # 'in' on a list
        {"op": "contains", "field": "custom.seats", "value": 1},  # contains on a number
        {"op": "eq", "field": "custom.a.b", "value": 1},  # nested custom path
        {"op": "weird", "field": "email", "value": 1},  # unknown op (validate_predicate)
        # Event fields are not supported until P1.9 (event_rollups) — must be rejected up front.
        {"op": "gte", "field": "event.purchased.count", "value": 3},
        {"op": "exists", "field": "event.purchased.count"},
    ],
)
def test_invalid_predicates_raise(predicate: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        compile_contact_where(predicate, _ATTRS)
