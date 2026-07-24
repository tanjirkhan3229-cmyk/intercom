"""Unit tests for the custom-view predicate→SQL compiler (P1.7 S3).

Pure (no DB): asserts the ``ConversationViewResolver`` field allowlist + coercion reject malformed
/ hostile filters at compile time, and that well-formed ASTs compile to a SQL expression. The
allowlist + parameter binding are the injection defence — an unknown/hostile ``field`` never
reaches SQL.
"""

from __future__ import annotations

import pytest
from sqlalchemy import ColumnElement

from relay.core.errors import ValidationError
from relay.modules.messaging.views import compile_view_where


def test_empty_filter_matches_all() -> None:
    assert isinstance(compile_view_where({}), ColumnElement)
    assert isinstance(compile_view_where(None), ColumnElement)


def test_valid_ast_compiles() -> None:
    ast = {
        "op": "and",
        "clauses": [
            {"op": "eq", "field": "channel", "value": "email"},
            {"op": "eq", "field": "priority", "value": True},
            {"op": "in", "field": "state", "value": ["open", "snoozed"]},
            {"op": "exists", "field": "waiting_since"},
            {"op": "gt", "field": "created_at", "value": "2021-01-01T00:00:00+00:00"},
            {"op": "eq", "field": "attributes.tier", "value": "gold"},
            {"op": "not", "clause": {"op": "eq", "field": "ai_status", "value": "handed_off"}},
        ],
    }
    assert isinstance(compile_view_where(ast), ColumnElement)


@pytest.mark.parametrize(
    "ast",
    [
        {"op": "eq", "field": "nonsense", "value": 1},  # unknown field
        {"op": "eq", "field": "state; DROP TABLE conversations", "value": "x"},  # injection attempt
        {"op": "gt", "field": "state", "value": "x"},  # ordered op on a non-datetime field
        {"op": "lt", "field": "priority", "value": True},  # ordered op on a boolean
        {"op": "eq", "field": "attributes.a.b", "value": "x"},  # nested attribute path
        {"op": "contains", "field": "priority", "value": "x"},  # contains on a boolean
        {"op": "eq", "field": "priority", "value": "notabool"},  # uncoercible boolean
        {"op": "eq", "field": "team_id", "value": "not-a-public-id"},  # undecodable id
        {"op": "gte", "field": "created_at", "value": "not-a-date"},  # uncoercible datetime
        {"op": "in", "field": "state", "value": "not-a-list"},  # 'in' needs a list
    ],
)
def test_rejects_bad_or_hostile_filters(ast: dict) -> None:
    with pytest.raises(ValidationError):
        compile_view_where(ast)
