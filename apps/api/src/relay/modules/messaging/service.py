"""Service layer for the ``messaging`` module — the cross-module interface (RFC-002 §5.3).

The heart is **W1**: appending a part is one transaction that (1) inserts the
``conversation_part``, (2) updates the conversation head (``last_part_at`` always;
``waiting_since`` set on a contact comment, cleared on an agent comment), and (3) writes
outbox row(s) for downstream effects — no cross-table fan-out inside the txn (RFC-002 §5.3,
RFC-001 §6.5).

Ordering + races (RFC-002 §7):
- Same-conversation writes serialise on a ``SELECT … FOR UPDATE`` of the head, so the outbox
  ``seq`` is monotonic per conversation (``relay.core.outbox.emit`` relies on this).
- Assignment claims are atomic: ``UPDATE … WHERE assignee_id IS NULL RETURNING`` — no
  serializable isolation, no double-assignment.

State machine: transitions are validated here (service layer) and the ``snooze_shape`` CHECK
guards the state's shape at the DB layer.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from relay.core import outbox
from relay.core.errors import ConflictError, NotFoundError, ValidationError
from relay.core.ids import IdPrefix, decode_public_id, encode_public_id, uuid7
from relay.core.pagination import Page, clamp_limit
from relay.core.principal import Principal
from relay.core.rbac import Role, authorize
from relay.core.redis import get_redis
from relay.modules.identity import service as identity_service

from . import events, schemas
from .models import Conversation, ConversationPart, ConversationTag, SavedReply

# Round-robin balance counters live in Redis (RFC-002 §7); DB reconciliation is out of P0 scope.
RR_COUNTER_PREFIX = "assign:rr:"

# The state machine (RFC-002 §5.3). Keys = current state, values = permitted targets.
VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"snoozed", "closed"}),
    "snoozed": frozenset({"open", "closed"}),
    "closed": frozenset({"open"}),
}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _decode_or_404(prefix: str, public_id: str, what: str) -> uuid.UUID:
    try:
        return decode_public_id(prefix, public_id)
    except ValueError as exc:
        raise NotFoundError(f"{what} not found") from exc


# --- DTO builders -------------------------------------------------------------


def conversation_out(c: Conversation) -> schemas.ConversationOut:
    return schemas.ConversationOut(
        id=encode_public_id(IdPrefix.CONVERSATION, c.id),
        contact_id=encode_public_id(IdPrefix.CONTACT, c.contact_id),
        channel=c.channel,
        state=c.state,
        assignee_id=encode_public_id(IdPrefix.ADMIN, c.assignee_id) if c.assignee_id else None,
        team_id=encode_public_id(IdPrefix.TEAM, c.team_id) if c.team_id else None,
        priority=c.priority,
        waiting_since=c.waiting_since,
        snoozed_until=c.snoozed_until,
        last_part_at=c.last_part_at,
        first_contact_reply_at=c.first_contact_reply_at,
        ai_status=c.ai_status,
        created_at=c.created_at,
    )


def _author_public_id(part: ConversationPart) -> str | None:
    if part.author_id is None:
        return None
    if part.author_kind == "contact":
        return encode_public_id(IdPrefix.CONTACT, part.author_id)
    return encode_public_id(IdPrefix.ADMIN, part.author_id)  # admin | ai_agent


def part_out(p: ConversationPart) -> schemas.PartOut:
    return schemas.PartOut(
        id=encode_public_id(IdPrefix.PART, p.id),
        conversation_id=encode_public_id(IdPrefix.CONVERSATION, p.conversation_id),
        author_kind=p.author_kind,
        author_id=_author_public_id(p),
        part_type=p.part_type,
        body=p.body,
        attachments=p.attachments,
        meta=p.meta,
        created_at=p.created_at,
    )


def saved_reply_out(r: SavedReply) -> schemas.SavedReplyOut:
    return schemas.SavedReplyOut(
        id=encode_public_id(IdPrefix.SAVED_REPLY, r.id),
        shortcut=r.shortcut,
        title=r.title,
        body=r.body,
        created_at=r.created_at,
    )


# --- Event payloads (RFC-001 §6.5) --------------------------------------------


def _conversation_payload(c: Conversation) -> dict[str, Any]:
    return {
        "workspace_id": encode_public_id(IdPrefix.WORKSPACE, c.workspace_id),
        "conversation_id": encode_public_id(IdPrefix.CONVERSATION, c.id),
        "contact_id": encode_public_id(IdPrefix.CONTACT, c.contact_id),
        "state": c.state,
        "assignee_id": encode_public_id(IdPrefix.ADMIN, c.assignee_id) if c.assignee_id else None,
        "team_id": encode_public_id(IdPrefix.TEAM, c.team_id) if c.team_id else None,
    }


def _part_payload(c: Conversation, p: ConversationPart) -> dict[str, Any]:
    payload = _conversation_payload(c)
    payload.update(
        part_id=encode_public_id(IdPrefix.PART, p.id),
        part_type=p.part_type,
        author_kind=p.author_kind,
        created_at=p.created_at.isoformat(),
    )
    return payload


# --- Head loading -------------------------------------------------------------


async def _load_for_update(session: AsyncSession, conversation_id: uuid.UUID) -> Conversation:
    """Load a conversation head with a row lock (serialises same-conversation W1 → monotonic
    outbox seq; RLS scopes the row to the workspace)."""
    conv = (
        await session.execute(
            select(Conversation).where(Conversation.id == conversation_id).with_for_update()
        )
    ).scalar_one_or_none()
    if conv is None:
        raise NotFoundError("conversation not found")
    return conv


async def _get(session: AsyncSession, conversation_id: uuid.UUID) -> Conversation:
    conv = await session.get(Conversation, conversation_id)
    if conv is None:
        raise NotFoundError("conversation not found")
    return conv


async def _load_conv(session: AsyncSession, public_id: str) -> Conversation:
    """Decode a public id and load the head FOR UPDATE (404 if it doesn't exist for the tenant)."""
    return await _load_for_update(
        session, _decode_or_404(IdPrefix.CONVERSATION, public_id, "conversation")
    )


# --- W1: append a part (insert part → update head → outbox) -------------------


async def _append_part(
    session: AsyncSession,
    conv: Conversation,
    *,
    author_kind: str,
    author_id: uuid.UUID | None,
    part_type: str,
    body: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    channel_meta: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> ConversationPart:
    """The W1 core. ``conv`` must already be lock-held (freshly inserted, or FOR UPDATE)."""
    now = _now()

    # waiting_since rules (RFC-002 §5.3 / the prompt): set on a contact comment (start the SLA
    # clock, keep the earliest), cleared on an agent comment. Other part types don't touch it.
    if part_type == "comment":
        if author_kind == "contact":
            if conv.waiting_since is None:
                conv.waiting_since = now
            if conv.first_contact_reply_at is None:
                conv.first_contact_reply_at = now
        elif author_kind in ("admin", "ai_agent"):
            conv.waiting_since = None
    conv.last_part_at = now

    part = ConversationPart(
        id=uuid7(),
        workspace_id=conv.workspace_id,
        conversation_id=conv.id,
        author_kind=author_kind,
        author_id=author_id,
        part_type=part_type,
        body=body,
        attachments=attachments or [],
        channel_meta=channel_meta or {},
        meta=meta or {},
        created_at=now,
    )
    session.add(part)
    # Flush the head UPDATE + part INSERT before emitting so the row lock is held when emit
    # computes the next per-aggregate seq (see relay.core.outbox.emit).
    await session.flush()
    await outbox.emit(
        session,
        aggregate=events.AGGREGATE_CONVERSATION,
        aggregate_id=conv.id,
        topic=events.CONVERSATION_PART_CREATED,
        payload=_part_payload(conv, part),
    )
    return part


# --- Conversations: create + list + read --------------------------------------


async def create_conversation(
    session: AsyncSession, principal: Principal, req: schemas.ConversationCreate
) -> schemas.ConversationOut:
    """Open a conversation with the contact's first message (a contact ``comment``).

    Emits ``conversation.created`` then ``conversation.part.created`` — both on the conversation
    aggregate, so their outbox seqs order the thread from the start.
    """
    authorize(principal, min_role=Role.AGENT)
    contact_id = _decode_or_404(IdPrefix.CONTACT, req.contact_id, "contact")
    team_id = _decode_or_404(IdPrefix.TEAM, req.team_id, "team") if req.team_id else None

    now = _now()
    conv = Conversation(
        id=uuid7(),
        workspace_id=principal.workspace_id,
        contact_id=contact_id,
        channel=req.channel,
        team_id=team_id,
        state="open",
        last_part_at=now,
    )
    session.add(conv)
    try:
        await session.flush()  # locks the new head row; validates the contact/team FKs
    except sa.exc.IntegrityError as exc:
        raise ValidationError("unknown contact or team") from exc

    await outbox.emit(
        session,
        aggregate=events.AGGREGATE_CONVERSATION,
        aggregate_id=conv.id,
        topic=events.CONVERSATION_CREATED,
        payload=_conversation_payload(conv),
    )
    await _append_part(
        session,
        conv,
        author_kind="contact",
        author_id=contact_id,
        part_type="comment",
        body=req.body,
        attachments=req.attachments,
        channel_meta=req.channel_meta,
    )
    return conversation_out(conv)


def _encode_cursor(c: Conversation) -> str:
    ts = c.waiting_since.isoformat() if c.waiting_since is not None else ""
    return f"{ts}|{encode_public_id(IdPrefix.CONVERSATION, c.id)}"


def _decode_cursor(cursor: str) -> tuple[dt.datetime | None, uuid.UUID]:
    ts_str, _, pid = cursor.partition("|")
    w = dt.datetime.fromisoformat(ts_str) if ts_str else None
    return w, _decode_or_404(IdPrefix.CONVERSATION, pid, "cursor")


async def list_conversations(
    session: AsyncSession,
    *,
    state: str = "open",
    team_id: str | None = None,
    assignee_id: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
) -> Page[schemas.ConversationOut]:
    """R1 inbox view — open conversations for a team/assignee, ordered by ``waiting_since``.

    The base query (no cursor) is exactly the RFC-002 §6 R1 shape and is served by the partial
    index ``conv_open_team`` / ``conv_open_asgn`` (Index Scan, no Sort — proven by an EXPLAIN
    test). Keyset pagination adds ``id`` as a tiebreak and walks NULL-``waiting_since`` rows last.
    """
    n = clamp_limit(limit)
    stmt = select(Conversation).where(Conversation.state == state)
    if team_id is not None:
        stmt = stmt.where(Conversation.team_id == _decode_or_404(IdPrefix.TEAM, team_id, "team"))
    if assignee_id is not None:
        stmt = stmt.where(
            Conversation.assignee_id == _decode_or_404(IdPrefix.ADMIN, assignee_id, "assignee")
        )
    stmt = stmt.order_by(Conversation.waiting_since.asc().nullslast(), Conversation.id.asc())

    if cursor:
        w, i = _decode_cursor(cursor)
        if w is not None:
            stmt = stmt.where(
                sa.or_(
                    Conversation.waiting_since > w,
                    sa.and_(Conversation.waiting_since == w, Conversation.id > i),
                    Conversation.waiting_since.is_(None),
                )
            )
        else:  # already in the NULLS-LAST region; walk it by id
            stmt = stmt.where(Conversation.waiting_since.is_(None), Conversation.id > i)

    convs = list((await session.scalars(stmt.limit(n + 1))).all())
    next_cursor = None
    if len(convs) > n:
        convs = convs[:n]
        next_cursor = _encode_cursor(convs[-1])
    return Page(items=[conversation_out(c) for c in convs], next_cursor=next_cursor)


async def get_conversation(session: AsyncSession, public_id: str) -> schemas.ConversationOut:
    cid = _decode_or_404(IdPrefix.CONVERSATION, public_id, "conversation")
    return conversation_out(await _get(session, cid))


async def list_parts(
    session: AsyncSession, public_id: str, *, cursor: str | None = None, limit: int | None = None
) -> Page[schemas.PartOut]:
    """R2 thread page — newest-first keyset on ``parts_thread (conversation_id, id)``."""
    cid = _decode_or_404(IdPrefix.CONVERSATION, public_id, "conversation")
    n = clamp_limit(limit)
    stmt = select(ConversationPart).where(ConversationPart.conversation_id == cid)
    if cursor:
        cur = _decode_or_404(IdPrefix.PART, cursor, "cursor")
        stmt = stmt.where(ConversationPart.id < cur)
    stmt = stmt.order_by(ConversationPart.id.desc()).limit(n + 1)
    parts = list((await session.scalars(stmt)).all())
    next_cursor = None
    if len(parts) > n:
        parts = parts[:n]
        next_cursor = encode_public_id(IdPrefix.PART, parts[-1].id)
    return Page(items=[part_out(p) for p in parts], next_cursor=next_cursor)


# --- Comments + notes + rating (W1) -------------------------------------------


async def add_reply(
    session: AsyncSession, principal: Principal, public_id: str, req: schemas.ReplyIn
) -> schemas.PartOut:
    """Agent reply — an ``admin`` ``comment``. Clears ``waiting_since`` (the agent answered)."""
    authorize(principal, min_role=Role.AGENT)
    conv = await _load_conv(session, public_id)
    part = await _append_part(
        session,
        conv,
        author_kind="admin",
        author_id=principal.admin_id,
        part_type="comment",
        body=req.body,
        attachments=req.attachments,
    )
    return part_out(part)


async def add_note(
    session: AsyncSession, principal: Principal, public_id: str, req: schemas.NoteIn
) -> schemas.PartOut:
    """Internal note — an ``admin`` ``note``. Does not touch ``waiting_since`` (not a reply)."""
    authorize(principal, min_role=Role.AGENT)
    conv = await _load_conv(session, public_id)
    part = await _append_part(
        session,
        conv,
        author_kind="admin",
        author_id=principal.admin_id,
        part_type="note",
        body=req.body,
    )
    return part_out(part)


async def add_rating(
    session: AsyncSession, principal: Principal, public_id: str, req: schemas.RatingIn
) -> schemas.PartOut:
    """A conversation rating (submitted by the contact; the widget wires the real path in P0.6)."""
    authorize(principal, min_role=Role.AGENT)
    conv = await _load_conv(session, public_id)
    part = await _append_part(
        session,
        conv,
        author_kind="contact",
        author_id=conv.contact_id,
        part_type="rating",
        body=req.remark,
        meta={"rating": req.rating},
    )
    return part_out(part)


# --- State machine (W4) -------------------------------------------------------


async def change_state(
    session: AsyncSession, principal: Principal, public_id: str, req: schemas.StateChangeIn
) -> schemas.ConversationOut:
    """Snooze / close / (re)open. Rejects invalid transitions (service) — the DB ``snooze_shape``
    CHECK is the second guard (a snoozed row must carry ``snoozed_until``)."""
    authorize(principal, min_role=Role.AGENT)
    conv = await _load_conv(session, public_id)
    target = req.state
    if target not in VALID_TRANSITIONS.get(conv.state, frozenset()):
        raise ConflictError(
            f"cannot move conversation from '{conv.state}' to '{target}'",
            details={"from": conv.state, "to": target},
        )

    prev = conv.state
    if target == "snoozed":
        if req.snoozed_until is None:
            raise ValidationError("snoozing requires snoozed_until")
        conv.snoozed_until = req.snoozed_until
        conv.waiting_since = None
    elif target == "closed":
        conv.snoozed_until = None
        conv.waiting_since = None
    else:  # open (reopen / wake)
        conv.snoozed_until = None
    conv.state = target

    await _append_part(
        session,
        conv,
        author_kind="admin",
        author_id=principal.admin_id,
        part_type="state_change",
        meta={"from": prev, "to": target},
    )
    await outbox.emit(
        session,
        aggregate=events.AGGREGATE_CONVERSATION,
        aggregate_id=conv.id,
        topic=events.CONVERSATION_STATE_CHANGED,
        payload={**_conversation_payload(conv), "from": prev, "to": target},
    )
    return conversation_out(conv)


# --- Assignment (W4) ----------------------------------------------------------


async def _record_assignment(
    session: AsyncSession, principal: Principal, conv: Conversation
) -> None:
    await _append_part(
        session,
        conv,
        author_kind="admin",
        author_id=principal.admin_id,
        part_type="assignment",
        meta={
            "assignee_id": encode_public_id(IdPrefix.ADMIN, conv.assignee_id)
            if conv.assignee_id
            else None,
            "team_id": encode_public_id(IdPrefix.TEAM, conv.team_id) if conv.team_id else None,
        },
    )
    await outbox.emit(
        session,
        aggregate=events.AGGREGATE_CONVERSATION,
        aggregate_id=conv.id,
        topic=events.CONVERSATION_ASSIGNED,
        payload=_conversation_payload(conv),
    )


async def assign(
    session: AsyncSession, principal: Principal, public_id: str, req: schemas.AssignIn
) -> schemas.ConversationOut:
    """Manual assignment: set assignee and/or team (overrides any current assignee)."""
    authorize(principal, min_role=Role.AGENT)
    if req.assignee_id is None and req.team_id is None:
        raise ValidationError("assign requires assignee_id and/or team_id")
    conv = await _load_conv(session, public_id)
    if req.assignee_id is not None:
        conv.assignee_id = _decode_or_404(IdPrefix.ADMIN, req.assignee_id, "assignee")
    if req.team_id is not None:
        conv.team_id = _decode_or_404(IdPrefix.TEAM, req.team_id, "team")
    await _record_assignment(session, principal, conv)
    return conversation_out(conv)


async def claim(
    session: AsyncSession, principal: Principal, public_id: str
) -> schemas.ConversationOut:
    """Atomically claim an *unassigned* conversation for the acting agent (RFC-002 §7):
    ``UPDATE … WHERE assignee_id IS NULL RETURNING``. If already assigned, it's a no-op."""
    authorize(principal, min_role=Role.AGENT)
    cid = _decode_or_404(IdPrefix.CONVERSATION, public_id, "conversation")
    claimed = (
        await session.execute(
            update(Conversation)
            .where(Conversation.id == cid, Conversation.assignee_id.is_(None))
            .values(assignee_id=principal.admin_id)
            .returning(Conversation.id)
        )
    ).scalar_one_or_none()

    conv = await _load_for_update(session, cid)  # 404 if it doesn't exist for this workspace
    if claimed is not None:
        await _record_assignment(session, principal, conv)
    return conversation_out(conv)


async def assign_round_robin(
    session: AsyncSession, principal: Principal, public_id: str, req: schemas.RoundRobinIn
) -> schemas.ConversationOut:
    """Round-robin assign to the next agent in a team (RFC-002 §7). Picks via a Redis counter,
    then claims atomically (``WHERE assignee_id IS NULL``) so a concurrent claim can't double up."""
    authorize(principal, min_role=Role.AGENT)
    cid = _decode_or_404(IdPrefix.CONVERSATION, public_id, "conversation")
    team_id = _decode_or_404(IdPrefix.TEAM, req.team_id, "team")

    agents = await identity_service.team_agent_ids(session, team_id)
    if not agents:
        raise ValidationError("team has no assignable agents")

    redis = get_redis()
    n = await redis.incr(f"{RR_COUNTER_PREFIX}{principal.workspace_id}:{team_id}")
    chosen = agents[(int(n) - 1) % len(agents)]

    claimed = (
        await session.execute(
            update(Conversation)
            .where(Conversation.id == cid, Conversation.assignee_id.is_(None))
            .values(assignee_id=chosen, team_id=team_id)
            .returning(Conversation.id)
        )
    ).scalar_one_or_none()

    conv = await _load_for_update(session, cid)
    if claimed is not None:
        await _record_assignment(session, principal, conv)
    return conversation_out(conv)


# --- Tags ---------------------------------------------------------------------


async def add_tag(
    session: AsyncSession, principal: Principal, public_id: str, req: schemas.TagIn
) -> None:
    authorize(principal, min_role=Role.AGENT)
    cid = _decode_or_404(IdPrefix.CONVERSATION, public_id, "conversation")
    await _get(session, cid)  # 404 if missing for this workspace
    stmt = (
        pg_insert(ConversationTag)
        .values(workspace_id=principal.workspace_id, conversation_id=cid, name=req.name)
        .on_conflict_do_nothing(
            index_elements=[
                ConversationTag.workspace_id,
                ConversationTag.conversation_id,
                ConversationTag.name,
            ]
        )
    )
    await session.execute(stmt)
    await session.flush()


async def remove_tag(
    session: AsyncSession, principal: Principal, public_id: str, name: str
) -> None:
    authorize(principal, min_role=Role.AGENT)
    cid = _decode_or_404(IdPrefix.CONVERSATION, public_id, "conversation")
    await session.execute(
        sa.delete(ConversationTag).where(
            ConversationTag.conversation_id == cid, ConversationTag.name == name
        )
    )
    await session.flush()


async def list_tags(session: AsyncSession, public_id: str) -> list[schemas.TagOut]:
    cid = _decode_or_404(IdPrefix.CONVERSATION, public_id, "conversation")
    names = (
        await session.scalars(
            select(ConversationTag.name)
            .where(ConversationTag.conversation_id == cid)
            .order_by(ConversationTag.name)
        )
    ).all()
    return [schemas.TagOut(name=name) for name in names]


# --- Saved replies (macros) ---------------------------------------------------


async def create_saved_reply(
    session: AsyncSession, principal: Principal, req: schemas.SavedReplyCreate
) -> schemas.SavedReplyOut:
    authorize(principal, min_role=Role.ADMIN)
    reply = SavedReply(
        workspace_id=principal.workspace_id, shortcut=req.shortcut, title=req.title, body=req.body
    )
    session.add(reply)
    try:
        await session.flush()
    except sa.exc.IntegrityError as exc:
        raise ConflictError("a saved reply with this shortcut already exists") from exc
    return saved_reply_out(reply)


async def list_saved_replies(session: AsyncSession) -> list[schemas.SavedReplyOut]:
    replies = (await session.scalars(select(SavedReply).order_by(SavedReply.shortcut))).all()
    return [saved_reply_out(r) for r in replies]


async def delete_saved_reply(session: AsyncSession, principal: Principal, public_id: str) -> None:
    authorize(principal, min_role=Role.ADMIN)
    rid = _decode_or_404(IdPrefix.SAVED_REPLY, public_id, "saved reply")
    reply = await session.get(SavedReply, rid)
    if reply is None:
        raise NotFoundError("saved reply not found")
    await session.delete(reply)
    await session.flush()
