"""Custom inbox views — saved conversation filters (P1.7 S3).

A view stores a predicates AST (``core.predicates``) compiled here to a ``conversations`` WHERE by
:class:`ConversationViewResolver` — a ``SqlLeafResolver`` bound to the conversation head, mirroring
``crm.audience.ContactAudienceResolver``. CRUD validates + compiles the AST on save (a bad field is
a 422 up front, not at query time); listing reuses the R1 keyset ordering
(:func:`service.list_conversations_where`); live counts are cached in Redis (the P0.9 R4 idiom).

Injection safety: field names resolve to real ``Conversation`` columns or a fixed JSONB path, and
values bind as SQLAlchemy parameters — a hostile ``field``/``value`` cannot inject SQL. Semantics
match ``core.predicates.evaluate``: ``eq`` excludes NULL, ``ne`` includes it (``IS DISTINCT FROM``),
ordered/`in` exclude NULL, ``exists`` treats a JSON null as absent.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, cast

from sqlalchemy import ColumnElement, and_, false, not_, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import select

from relay.core.errors import NotFoundError, ValidationError
from relay.core.ids import IdPrefix, decode_public_id, encode_public_id
from relay.core.pagination import Page
from relay.core.predicates import to_sql, validate_predicate
from relay.core.principal import Principal
from relay.core.rbac import Role, authorize
from relay.core.redis import get_redis

from . import schemas, service
from .models import Conversation, InboxView

VIEW_COUNT_CACHE_PREFIX = "inbox:view:count:"
VIEW_COUNT_TTL_SECONDS = 10

# Allowlisted conversation fields → type tag. Anything else must be ``attributes.<key>``.
_TEXT_FIELDS = frozenset({"state", "channel", "ai_status"})
_BOOL_FIELDS = frozenset({"priority"})
_DATETIME_FIELDS = frozenset(
    {"waiting_since", "snoozed_until", "last_part_at", "first_contact_reply_at", "created_at"}
)
# id fields carry a public-id value that must be decoded to the stored uuid.
_ID_FIELDS: dict[str, str] = {"team_id": IdPrefix.TEAM, "assignee_id": IdPrefix.ADMIN}

_COLUMNS: dict[str, Any] = {
    "state": Conversation.state,
    "channel": Conversation.channel,
    "ai_status": Conversation.ai_status,
    "priority": Conversation.priority,
    "team_id": Conversation.team_id,
    "assignee_id": Conversation.assignee_id,
    "waiting_since": Conversation.waiting_since,
    "snoozed_until": Conversation.snoozed_until,
    "last_part_at": Conversation.last_part_at,
    "first_contact_reply_at": Conversation.first_contact_reply_at,
    "created_at": Conversation.created_at,
}

_ORDERED_OPS = frozenset({"gt", "gte", "lt", "lte"})


def _bool(expr: Any) -> ColumnElement[bool]:
    return cast("ColumnElement[bool]", expr)


def _decode_or_404(prefix: str, public_id: str, what: str) -> uuid.UUID:
    try:
        return decode_public_id(prefix, public_id)
    except ValueError as exc:
        raise NotFoundError(f"{what} not found") from exc


class _Resolved:
    __slots__ = ("attr_key", "expr", "tag")

    def __init__(self, *, expr: Any, tag: str, attr_key: str | None = None) -> None:
        self.expr = expr
        self.tag = tag
        self.attr_key = attr_key


def _resolve(field: str) -> _Resolved:
    if field in _COLUMNS:
        col = _COLUMNS[field]
        if field in _TEXT_FIELDS:
            return _Resolved(expr=col, tag="text")
        if field in _BOOL_FIELDS:
            return _Resolved(expr=col, tag="boolean")
        if field in _DATETIME_FIELDS:
            return _Resolved(expr=col, tag="datetime")
        return _Resolved(expr=col, tag=f"id:{field}")  # team_id / assignee_id
    if field.startswith("attributes."):
        key = field[len("attributes.") :]
        if not key or "." in key:
            raise ValidationError(f"invalid attribute field {field!r}")
        # Compared as text via the JSONB ``->>`` accessor.
        return _Resolved(expr=Conversation.attributes[key].astext, tag="attr", attr_key=key)
    raise ValidationError(f"field {field!r} is not a filterable conversation field")


def _coerce(resolved: _Resolved, value: Any) -> Any:
    tag = resolved.tag
    if tag in ("text", "attr"):
        if isinstance(value, bool) or not isinstance(value, str | int | float):
            raise ValidationError("expected a scalar value", details={"value": value})
        return value if isinstance(value, str) else str(value)
    if tag == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        raise ValidationError("expected a boolean value", details={"value": value})
    if tag == "datetime":
        if isinstance(value, dt.datetime):
            return value
        try:
            return dt.datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise ValidationError("expected an ISO datetime", details={"value": value}) from exc
    if tag.startswith("id:"):
        field = tag[len("id:") :]
        if not isinstance(value, str):
            raise ValidationError(f"{field} must be a public id string", details={"value": value})
        try:
            return decode_public_id(_ID_FIELDS[field], value)
        except ValueError as exc:
            raise ValidationError(f"invalid {field}", details={"value": value}) from exc
    raise ValidationError(f"unsupported field type {tag!r}")


class ConversationViewResolver:
    """A ``core.predicates.SqlLeafResolver`` over the ``conversations`` head."""

    def compare(self, op: str, field: str, value: Any) -> ColumnElement[bool]:
        resolved = _resolve(field)
        if op in _ORDERED_OPS and resolved.tag != "datetime":
            raise ValidationError(f"'{op}' is only supported on datetime fields, not {field!r}")
        val = _coerce(resolved, value)
        expr = resolved.expr
        if op == "eq":
            return _bool(expr == val)
        if op == "ne":
            return _bool(expr.is_distinct_from(val))  # NULL/missing counts as "not equal"
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
        resolved = _resolve(field)
        if op == "in":
            if not isinstance(value, list):
                raise ValidationError("'in' requires a list value")
            return _bool(resolved.expr.in_([_coerce(resolved, v) for v in value]))
        # contains — substring match on text/attribute fields only.
        if resolved.tag not in ("text", "attr"):
            raise ValidationError(f"'contains' is only supported on text fields, not {field!r}")
        return _bool(resolved.expr.contains(str(value), autoescape=True))

    def presence(self, op: str, field: str) -> ColumnElement[bool]:
        resolved = _resolve(field)
        if resolved.tag == "attr":
            assert resolved.attr_key is not None
            present = _bool(
                and_(
                    Conversation.attributes.has_key(resolved.attr_key),
                    Conversation.attributes[resolved.attr_key].astext.isnot(None),
                )
            )
        else:
            present = _bool(resolved.expr.isnot(None))
        return present if op == "exists" else not_(present)


def compile_view_where(filter_ast: dict[str, Any] | None) -> ColumnElement[bool]:
    """Compile a saved view filter to a ``conversations`` WHERE clause. An empty/absent filter
    matches everything. Raises ``ValidationError`` for a malformed AST or a disallowed field."""
    if not filter_ast:
        return true()
    validate_predicate(filter_ast)
    compiled = to_sql(filter_ast, ConversationViewResolver())
    return compiled if compiled is not None else false()


# --- DTO ----------------------------------------------------------------------


def view_out(v: InboxView) -> schemas.InboxViewOut:
    return schemas.InboxViewOut(
        id=encode_public_id(IdPrefix.INBOX_VIEW, v.id),
        name=v.name,
        filter=v.filter or {},
        team_id=encode_public_id(IdPrefix.TEAM, v.team_id) if v.team_id else None,
        created_at=v.created_at,
        updated_at=v.updated_at,
    )


# --- CRUD ---------------------------------------------------------------------


async def create_view(
    session: AsyncSession, principal: Principal, req: schemas.InboxViewIn
) -> schemas.InboxViewOut:
    """Create a saved view (agent+). The filter is validated + compiled here so a bad field/AST is
    rejected on save, not at query time."""
    authorize(principal, min_role=Role.AGENT)
    compile_view_where(req.filter)  # 422 on a malformed AST / disallowed field
    team_id = _decode_or_404(IdPrefix.TEAM, req.team_id, "team") if req.team_id else None
    view = InboxView(
        workspace_id=principal.workspace_id,
        name=req.name,
        filter=req.filter,
        team_id=team_id,
        created_by=principal.admin_id,
    )
    session.add(view)
    try:
        await session.flush()
    except Exception:  # pragma: no cover - unknown team FK
        raise ValidationError("unknown team") from None
    return view_out(view)


async def update_view(
    session: AsyncSession, principal: Principal, public_id: str, req: schemas.InboxViewIn
) -> schemas.InboxViewOut:
    authorize(principal, min_role=Role.AGENT)
    compile_view_where(req.filter)
    view = await session.get(InboxView, _decode_or_404(IdPrefix.INBOX_VIEW, public_id, "view"))
    if view is None:
        raise NotFoundError("view not found")
    view.name = req.name
    view.filter = req.filter
    view.team_id = _decode_or_404(IdPrefix.TEAM, req.team_id, "team") if req.team_id else None
    view.updated_at = dt.datetime.now(dt.UTC)  # explicit (avoid onupdate expiry → MissingGreenlet)
    await session.flush()
    return view_out(view)


async def list_views(session: AsyncSession) -> list[schemas.InboxViewOut]:
    rows = (await session.scalars(select(InboxView).order_by(InboxView.created_at))).all()
    return [view_out(v) for v in rows]


async def get_view(session: AsyncSession, public_id: str) -> schemas.InboxViewOut:
    view = await session.get(InboxView, _decode_or_404(IdPrefix.INBOX_VIEW, public_id, "view"))
    if view is None:
        raise NotFoundError("view not found")
    return view_out(view)


async def delete_view(session: AsyncSession, principal: Principal, public_id: str) -> None:
    authorize(principal, min_role=Role.AGENT)
    view = await session.get(InboxView, _decode_or_404(IdPrefix.INBOX_VIEW, public_id, "view"))
    if view is None:
        raise NotFoundError("view not found")
    await session.delete(view)
    await session.flush()


# --- query (filtered list + cached count) -------------------------------------


async def _load_view(session: AsyncSession, public_id: str) -> InboxView:
    view = await session.get(InboxView, _decode_or_404(IdPrefix.INBOX_VIEW, public_id, "view"))
    if view is None:
        raise NotFoundError("view not found")
    return view


async def list_conversations_by_view(
    session: AsyncSession, public_id: str, *, cursor: str | None = None, limit: int | None = None
) -> Page[schemas.ConversationOut]:
    view = await _load_view(session, public_id)
    where = compile_view_where(view.filter)
    return await service.list_conversations_where(session, where, cursor=cursor, limit=limit)


async def view_count(session: AsyncSession, public_id: str) -> schemas.ViewCountOut:
    """Live match count for a view, cached in Redis (~10 s TTL — the P0.9 R4 idiom) so the sidebar
    badge is O(1)."""
    view = await _load_view(session, public_id)
    redis = get_redis()
    cache_key = f"{VIEW_COUNT_CACHE_PREFIX}{encode_public_id(IdPrefix.INBOX_VIEW, view.id)}"
    cached = await redis.get(cache_key)
    if cached is not None:
        return schemas.ViewCountOut(count=int(cached))
    count = await service.count_conversations_where(session, compile_view_where(view.filter))
    await redis.set(cache_key, str(count), ex=VIEW_COUNT_TTL_SECONDS)
    return schemas.ViewCountOut(count=count)
