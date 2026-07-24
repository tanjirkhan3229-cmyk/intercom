"""Contact audience compiler: the predicate AST → parameterised SQL over ``contacts`` (P1.8).

Segments/audiences are the same JSON predicate grammar as workflow filters (``core.predicates``);
this is the SQL side of that grammar for the ``contacts`` table. It is used by outbound (P1.8) to
snapshot a broadcast audience. Event / rollup fields are **not** supported yet (``event_rollups``
lands in P1.9) — audiences span core contact fields + typed custom attributes only.

Injection safety: field names resolve to real ``Contact`` columns or a fixed JSONB path; values
are bound as SQLAlchemy parameters (never string-interpolated). An unknown field or a value that
cannot be coerced to its attribute's declared type raises ``ValidationError`` at compile time (so a
bad segment is rejected when the campaign/post is saved, not at fire time).

Semantics match ``core.predicates.evaluate`` so the SQL and the Python evaluator never disagree:
``eq`` excludes NULL/missing, ``ne`` includes it (``IS DISTINCT FROM``), ordered/`in` comparisons
exclude NULL, ``exists`` treats a JSON ``null`` as absent.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any, cast

from sqlalchemy import (
    Boolean,
    ColumnElement,
    DateTime,
    Numeric,
    and_,
    false,
    not_,
    true,
)

from relay.core.errors import ValidationError
from relay.core.predicates import to_sql, validate_predicate
from relay.modules.crm.models import Contact


def _bool(expr: Any) -> ColumnElement[bool]:
    """Narrow a SQLAlchemy comparison expression (typed ``Any`` on JSONB paths) to a bool column."""
    return cast("ColumnElement[bool]", expr)


# Allowlisted core contact fields → (column, type tag). Anything else must be ``custom.<key>``.
_CORE_FIELDS: dict[str, tuple[Any, str]] = {
    "email": (Contact.email, "text"),
    "name": (Contact.name, "text"),
    "phone": (Contact.phone, "text"),
    "kind": (Contact.kind, "text"),
    "external_id": (Contact.external_id, "text"),
    "last_seen_at": (Contact.last_seen_at, "datetime"),
    "created_at": (Contact.created_at, "datetime"),
}


class _ResolvedField:
    __slots__ = ("is_custom", "key", "tag")

    def __init__(self, *, is_custom: bool, key: str | None, tag: str) -> None:
        self.is_custom = is_custom
        self.key = key
        self.tag = tag


def _coerce(tag: str, value: Any) -> Any:
    """Coerce a bound value to the field's declared type (authoritative from attribute_definitions).

    Raises ``ValidationError`` if the value does not fit — a segment comparing a number attribute to
    a non-number is rejected up front rather than silently matching nothing.
    """
    try:
        if tag in ("text", "string"):
            if not isinstance(value, str):
                raise ValidationError("expected a string value", details={"value": value})
            return value
        if tag == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise ValidationError("expected a number value", details={"value": value})
            return float(value)
        if tag == "boolean":
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.lower() in ("true", "false"):
                return value.lower() == "true"
            raise ValidationError("expected a boolean value", details={"value": value})
        if tag in ("date", "datetime"):
            if isinstance(value, dt.datetime):
                return value
            return dt.datetime.fromisoformat(str(value))
    except (ValueError, TypeError) as exc:
        raise ValidationError(
            f"value {value!r} is not a valid {tag}", details={"value": value}
        ) from exc
    raise ValidationError(f"unsupported field type {tag!r}")


def _typed_expr(resolved: _ResolvedField) -> Any:
    """The SQL expression to compare against, cast to the field's declared type (SQLAlchemy
    JSONB-path expressions are ``Any``-typed; callers re-narrow the comparison via ``_bool``)."""
    if not resolved.is_custom:
        return _CORE_FIELDS[_core_name(resolved)][0]
    elem = Contact.custom[resolved.key]  # JSONB element accessor
    if resolved.tag == "string":
        return elem.astext
    if resolved.tag == "number":
        return elem.astext.cast(Numeric)
    if resolved.tag == "boolean":
        return elem.astext.cast(Boolean)
    if resolved.tag in ("date", "datetime"):
        return elem.astext.cast(DateTime(timezone=True))
    # "list" has no scalar comparison expression (only contains/exists).
    raise ValidationError(f"attribute type {resolved.tag!r} does not support this comparison")


def _core_name(resolved: _ResolvedField) -> str:
    # Only called for core fields; the key doubles as the field name there.
    assert resolved.key is not None
    return resolved.key


class ContactAudienceResolver:
    """A :class:`relay.core.predicates.SqlLeafResolver` bound to the ``contacts`` schema."""

    def __init__(self, attr_types: Mapping[str, str]) -> None:
        # custom attribute key -> data_type (string|number|boolean|date|list) from attr definitions
        self._attr_types = attr_types

    def _resolve(self, field: str) -> _ResolvedField:
        if field in _CORE_FIELDS:
            return _ResolvedField(is_custom=False, key=field, tag=_CORE_FIELDS[field][1])
        if field.startswith("custom."):
            key = field[len("custom.") :]
            if not key or "." in key:
                raise ValidationError(f"invalid custom field {field!r}")
            data_type = self._attr_types.get(key)
            if data_type is None:
                raise ValidationError(f"unknown contact attribute {key!r}")
            return _ResolvedField(is_custom=True, key=key, tag=data_type)
        if field.startswith("event."):
            # Event-count audiences require ``event_rollups`` (P1.9); rejected up front until then.
            raise ValidationError(f"event fields are not supported yet: {field!r}")
        raise ValidationError(f"field {field!r} is not an allowed audience field")

    def compare(self, op: str, field: str, value: Any) -> ColumnElement[bool]:
        resolved = self._resolve(field)
        if resolved.tag == "list":
            raise ValidationError(f"cannot use '{op}' on a list attribute")
        expr = _typed_expr(resolved)
        val = _coerce(resolved.tag, value)
        if op == "eq":
            return _bool(expr == val)
        if op == "ne":
            # NULL/missing counts as "not equal" (matches evaluate's ne semantics).
            return _bool(expr.is_distinct_from(val))
        if op == "gt":
            return _bool(expr > val)
        if op == "gte":
            return _bool(expr >= val)
        if op == "lt":
            return _bool(expr < val)
        if op == "lte":
            return _bool(expr <= val)
        raise ValidationError(f"unsupported comparison op {op!r}")

    def membership(self, op: str, field: str, value: Any) -> ColumnElement[bool]:
        resolved = self._resolve(field)
        if op == "in":
            if resolved.tag == "list":
                raise ValidationError("cannot use 'in' on a list attribute")
            if not isinstance(value, list):
                raise ValidationError("'in' requires a list value")
            expr = _typed_expr(resolved)
            return _bool(expr.in_([_coerce(resolved.tag, v) for v in value]))
        # contains
        if resolved.tag == "list":
            # JSONB array containment: custom[key] @> [value]
            return _bool(Contact.custom[resolved.key].contains([value]))
        if resolved.tag in ("text", "string"):
            expr = (
                _CORE_FIELDS[field][0]
                if not resolved.is_custom
                else Contact.custom[resolved.key].astext
            )
            # autoescape neutralises LIKE wildcards in the value (no % / _ injection).
            return _bool(expr.contains(str(value), autoescape=True))
        raise ValidationError(f"cannot use 'contains' on a {resolved.tag} attribute")

    def presence(self, op: str, field: str) -> ColumnElement[bool]:
        resolved = self._resolve(field)
        if not resolved.is_custom:
            present = _bool(_CORE_FIELDS[field][0].isnot(None))
        else:
            # Present iff the key exists AND the value is not JSON null (json null → NULL astext).
            present = _bool(
                and_(
                    Contact.custom.has_key(resolved.key),
                    Contact.custom[resolved.key].astext.isnot(None),
                )
            )
        return present if op == "exists" else not_(present)


def compile_contact_where(
    predicate: Mapping[str, Any] | None,
    attr_types: Mapping[str, str],
) -> ColumnElement[bool]:
    """Compile an audience predicate to a ``contacts`` WHERE clause (always excludes soft-deleted).

    An empty/absent predicate means "all contacts". Raises ``ValidationError`` for a malformed
    predicate or a disallowed field/value.
    """
    not_deleted = Contact.deleted_at.is_(None)
    if not predicate:
        return and_(not_deleted, true())
    validate_predicate(predicate)
    base = to_sql(predicate, ContactAudienceResolver(attr_types))
    return and_(not_deleted, base if base is not None else false())
