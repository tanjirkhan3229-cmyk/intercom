"""Predicate AST — a small JSON boolean-expression language (RFC-000 §2.7/§2.8).

A *predicate* is a JSON object evaluated against a flat-ish context ``Mapping`` to a bool.
Two subsystems share it (the prompt: workflow trigger-filters and condition branches are
"compiled from the same predicate AST as segments"):

- **automation** (P1.5): trigger-filters match against an event payload; condition nodes match
  against a run's accumulated context.
- **crm segments** (P1.9, not yet built): the same grammar will compile to SQL. This module owns
  the grammar + a Python evaluator now; a ``to_sql`` compiler is the future extension point, so
  the two never diverge.

Grammar (validated by :func:`validate_predicate`, evaluated by :func:`evaluate`)::

    {"op": "and", "clauses": [<predicate>, ...]}      # empty clauses → True
    {"op": "or",  "clauses": [<predicate>, ...]}      # empty clauses → False
    {"op": "not", "clause": <predicate>}
    {"op": "eq"|"ne"|"gt"|"gte"|"lt"|"lte", "field": "<dotted.path>", "value": <scalar>}
    {"op": "in",       "field": "<dotted.path>", "value": [<scalar>, ...]}
    {"op": "contains", "field": "<dotted.path>", "value": <scalar>}   # field is a list/str
    {"op": "exists"|"not_exists", "field": "<dotted.path>"}

``field`` is a dotted path resolved through nested mappings (``"a.b"`` → ``ctx["a"]["b"]``); a
missing/None value is well-defined for every op (never raises). Evaluation is total and
side-effect free, so it is safe to run on attacker-influenced payloads.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Protocol

from sqlalchemy import ColumnElement, and_, false, not_, or_, true

from relay.core.errors import ValidationError

LOGICAL_OPS: Final[frozenset[str]] = frozenset({"and", "or", "not"})
# Ops that compare a resolved ``field`` against a ``value``.
_VALUE_OPS: Final[frozenset[str]] = frozenset({"eq", "ne", "gt", "gte", "lt", "lte"})
_MEMBERSHIP_OPS: Final[frozenset[str]] = frozenset({"in", "contains"})
# Ops that only test presence (no ``value``).
_PRESENCE_OPS: Final[frozenset[str]] = frozenset({"exists", "not_exists"})
COMPARISON_OPS: Final[frozenset[str]] = _VALUE_OPS | _MEMBERSHIP_OPS | _PRESENCE_OPS
ALL_OPS: Final[frozenset[str]] = LOGICAL_OPS | COMPARISON_OPS

# Max nesting depth — a defence against a pathological/hostile graph blowing the stack.
_MAX_DEPTH: Final[int] = 32

# Sentinel distinct from a real ``None`` value in the context.
_MISSING: Final[Any] = object()


def _resolve(context: Mapping[str, Any], field: str) -> Any:
    """Resolve a dotted ``field`` path through nested mappings. Returns ``_MISSING`` if any
    segment is absent or a non-mapping is traversed."""
    cur: Any = context
    for part in field.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


# --- Validation ---------------------------------------------------------------


def validate_predicate(node: Any, *, _path: str = "predicate", _depth: int = 0) -> None:
    """Raise :class:`ValidationError` unless ``node`` is a well-formed predicate.

    Called at workflow-publish time so a malformed condition is rejected before it can ever run
    (the executor then trusts the stored graph). ``_path``/``_depth`` are internal.
    """
    if _depth > _MAX_DEPTH:
        raise ValidationError("predicate nested too deeply", details={"path": _path})
    if not isinstance(node, Mapping):
        raise ValidationError("predicate must be an object", details={"path": _path})
    op = node.get("op")
    if op not in ALL_OPS:
        raise ValidationError(f"unknown predicate op {op!r}", details={"path": _path, "op": op})

    if op in ("and", "or"):
        clauses = node.get("clauses")
        if not isinstance(clauses, list):
            raise ValidationError(
                f"'{op}' requires a 'clauses' list", details={"path": f"{_path}.clauses"}
            )
        for i, clause in enumerate(clauses):
            validate_predicate(clause, _path=f"{_path}.clauses[{i}]", _depth=_depth + 1)
        return
    if op == "not":
        if "clause" not in node:
            raise ValidationError("'not' requires a 'clause'", details={"path": _path})
        validate_predicate(node["clause"], _path=f"{_path}.clause", _depth=_depth + 1)
        return

    # Leaf comparison ops all require a string ``field``.
    field = node.get("field")
    if not isinstance(field, str) or not field:
        raise ValidationError(
            f"'{op}' requires a non-empty string 'field'", details={"path": _path}
        )
    if op in _PRESENCE_OPS:
        return
    if "value" not in node:
        raise ValidationError(f"'{op}' requires a 'value'", details={"path": _path})
    value = node["value"]
    if op == "in" and not isinstance(value, list):
        raise ValidationError("'in' requires a list 'value'", details={"path": _path})


# --- Evaluation ---------------------------------------------------------------


def _compare(op: str, left: Any, right: Any) -> bool:
    """Ordered comparison (gt/gte/lt/lte). Non-comparable operands (e.g. a missing field, or
    mismatched types) evaluate to ``False`` rather than raising — a filter never crashes a run."""
    if left is _MISSING or left is None:
        return False
    try:
        if op == "gt":
            return bool(left > right)
        if op == "gte":
            return bool(left >= right)
        if op == "lt":
            return bool(left < right)
        return bool(left <= right)  # lte
    except TypeError:
        return False


def evaluate(node: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
    """Evaluate a (previously validated) predicate against ``context``. Total + side-effect free.

    Defensive against a malformed node (returns ``False`` rather than raising) so a predicate that
    somehow bypassed validation can never wedge the executor.
    """
    op = node.get("op")

    if op == "and":
        return all(evaluate(c, context) for c in node.get("clauses", []))
    if op == "or":
        return any(evaluate(c, context) for c in node.get("clauses", []))
    if op == "not":
        clause = node.get("clause")
        return not evaluate(clause, context) if isinstance(clause, Mapping) else False

    field = node.get("field")
    if not isinstance(field, str):
        return False
    left = _resolve(context, field)

    if op == "exists":
        return left is not _MISSING and left is not None
    if op == "not_exists":
        return left is _MISSING or left is None

    value = node.get("value")
    if op == "eq":
        return left is not _MISSING and left == value
    if op == "ne":
        # A missing field is "not equal" to any concrete value (mirrors SQL's practical intent for
        # segment filters: "channel != email" should include rows with no channel).
        return left is _MISSING or left != value
    if op == "in":
        return left is not _MISSING and isinstance(value, list) and left in value
    if op == "contains":
        if left is _MISSING or left is None:
            return False
        try:
            return value in left  # list membership or substring
        except TypeError:
            return False
    if op in _VALUE_OPS:  # gt/gte/lt/lte
        return _compare(op, left, value)
    return False


# --- SQL compilation ----------------------------------------------------------
#
# ``to_sql`` compiles the SAME grammar to a SQLAlchemy boolean expression (the "future extension
# point" this module always promised) so segments/audiences never diverge from the Python
# evaluator above. Boolean composition (and/or/not, empty-clause identities) lives here so it
# stays canonical; a ``SqlLeafResolver`` (supplied by the caller — e.g. the outbound audience
# compiler) turns each leaf into a typed, parameterised expression over its own table. Values are
# never string-interpolated: the resolver binds them via SQLAlchemy expression objects, and field
# names resolve to real ``Column`` objects, so a hostile ``field``/``value`` cannot inject SQL.


class SqlLeafResolver(Protocol):
    """Turns a validated leaf predicate into a SQL boolean expression over a specific table.

    Implementations own the field allowlist + type coercion; a disallowed/unknown ``field`` must
    raise :class:`~relay.core.errors.ValidationError`. Semantics must match the Python evaluator:
    ``eq`` excludes NULL/missing, ``ne`` includes it, ordered/`in` comparisons exclude NULL.
    """

    def compare(self, op: str, field: str, value: Any) -> ColumnElement[bool]:
        """``eq``/``ne``/``gt``/``gte``/``lt``/``lte`` against ``value``."""
        ...

    def membership(self, op: str, field: str, value: Any) -> ColumnElement[bool]:
        """``in`` (value is a list) / ``contains`` (field is a list or string)."""
        ...

    def presence(self, op: str, field: str) -> ColumnElement[bool]:
        """``exists`` / ``not_exists``."""
        ...


def to_sql(node: Mapping[str, Any], resolver: SqlLeafResolver) -> ColumnElement[bool]:
    """Compile a (previously :func:`validate_predicate`-validated) predicate to a boolean SQL
    expression. Empty ``and`` → TRUE and empty ``or`` → FALSE, matching :func:`evaluate`.

    Call ``validate_predicate`` first; ``to_sql`` trusts the shape and raises only via the
    resolver (unknown field / uncoercible value).
    """
    op = node.get("op")

    if op == "and":
        clauses = node.get("clauses", [])
        return and_(true(), *(to_sql(c, resolver) for c in clauses))
    if op == "or":
        clauses = node.get("clauses", [])
        return or_(false(), *(to_sql(c, resolver) for c in clauses))
    if op == "not":
        return not_(to_sql(node["clause"], resolver))

    field = node["field"]
    if op in _PRESENCE_OPS:
        return resolver.presence(str(op), field)
    value = node.get("value")
    if op in _MEMBERSHIP_OPS:
        return resolver.membership(str(op), field, value)
    return resolver.compare(str(op), field, value)
