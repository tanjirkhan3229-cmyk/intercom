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
from dataclasses import dataclass
from typing import Any

import jwt
import sqlalchemy as sa
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from relay.core import outbox, realtime
from relay.core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from relay.core.ids import IdPrefix, decode_public_id, encode_public_id, uuid7
from relay.core.pagination import Page, clamp_limit
from relay.core.principal import ContactPrincipal, Principal
from relay.core.rbac import Role, authorize
from relay.core.redis import get_redis
from relay.core.security import (
    create_widget_session_token,
    decode_widget_session_token,
    verify_identity_hash,
)
from relay.modules.crm import service as crm_service
from relay.modules.identity import service as identity_service
from relay.settings import get_settings

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
        # Occurrence time of the event (ISO-8601). Consumers that must time a state change without
        # the part row (reporting P0.9) read this; realtime fan-out ignores it (RFC-001 §6.5).
        "occurred_at": _now().isoformat(),
    }


def _part_payload(c: Conversation, p: ConversationPart) -> dict[str, Any]:
    payload = _conversation_payload(c)
    payload.update(
        # ``channel`` lets channel-fanout consumers (P0.7 email) filter without re-reading the DB.
        # Additive to a stable contract — existing consumers (realtime, webhooks) ignore it.
        channel=c.channel,
        part_id=encode_public_id(IdPrefix.PART, p.id),
        part_type=p.part_type,
        author_kind=p.author_kind,
        created_at=p.created_at.isoformat(),
    )
    # Surface the CSAT score on the rating part so reporting can meter it off the event alone
    # (never scanning conversation_parts — P0.9 acceptance).
    if p.part_type == "rating":
        payload["rating"] = p.meta.get("rating")
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


async def _open_conversation(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    contact_id: uuid.UUID,
    channel: str,
    team_id: uuid.UUID | None,
    body: str,
    attachments: list[dict[str, Any]] | None = None,
    channel_meta: dict[str, Any] | None = None,
) -> Conversation:
    """Open a conversation with the contact's first message (a contact ``comment``).

    Emits ``conversation.created`` then ``conversation.part.created`` — both on the conversation
    aggregate, so their outbox seqs order the thread from the start. Shared by the agent create
    path and the widget's contact path so the head/outbox/``waiting_since`` rules never drift.
    """
    now = _now()
    conv = Conversation(
        id=uuid7(),
        workspace_id=workspace_id,
        contact_id=contact_id,
        channel=channel,
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
        body=body,
        attachments=attachments,
        channel_meta=channel_meta,
    )
    return conv


async def create_conversation(
    session: AsyncSession, principal: Principal, req: schemas.ConversationCreate
) -> schemas.ConversationOut:
    """Agent-authored conversation open (models a visitor message on the contact's behalf)."""
    authorize(principal, min_role=Role.AGENT)
    contact_id = _decode_or_404(IdPrefix.CONTACT, req.contact_id, "contact")
    team_id = _decode_or_404(IdPrefix.TEAM, req.team_id, "team") if req.team_id else None
    conv = await _open_conversation(
        session,
        workspace_id=principal.workspace_id,
        contact_id=contact_id,
        channel=req.channel,
        team_id=team_id,
        body=req.body,
        attachments=req.attachments,
        channel_meta=req.channel_meta,
    )
    return conversation_out(conv)


# --- Channel adapters: system-initiated inbound (no admin Principal) ----------
# The sanctioned cross-module entry points for channel modules (P0.7 email; import-linter allows
# ``channels -> messaging.service``). Inbound channel messages have no acting admin, so these take
# an explicit ``workspace_id`` (the adapter resolved the tenant + set the RLS GUC) instead of a
# ``Principal``, and run the SAME W1 as agent/contact writes — channels never re-implement W1 or
# touch messaging internals directly.


async def open_email_conversation(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    contact_id: uuid.UUID,
    channel_account_id: uuid.UUID | None,
    body: str | None,
    attachments: list[dict[str, Any]] | None = None,
    channel_meta: dict[str, Any] | None = None,
) -> Conversation:
    """Open an inbound *email* conversation with the contact's first message (a contact comment).

    Reuses the shared :func:`_open_conversation` W1 core (channel='email'), then stamps the
    resolving ``channel_account_id`` on the head so outbound replies know their sending account."""
    conv = await _open_conversation(
        session,
        workspace_id=workspace_id,
        contact_id=contact_id,
        channel="email",
        team_id=None,
        body=body or "",
        attachments=attachments,
        channel_meta=channel_meta,
    )
    conv.channel_account_id = channel_account_id
    await session.flush()
    return conv


async def append_contact_email(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    body: str | None,
    attachments: list[dict[str, Any]] | None = None,
    channel_meta: dict[str, Any] | None = None,
) -> ConversationPart:
    """Append an inbound contact email to an existing conversation (W1).

    A closed/snoozed thread is reopened **through the state machine** (appends a ``state_change``
    part + emits ``conversation.state_changed``) so the reporting spine sees the reopen — channels
    never hand-mutate ``conv.state``. RLS scopes the load to the caller's workspace; a foreign
    ``conversation_id`` raises NotFound."""
    conv = await _load_for_update(session, conversation_id)
    if conv.state != "open":
        prev = conv.state
        conv.state = "open"
        conv.snoozed_until = None
        await _append_part(
            session,
            conv,
            author_kind="system",
            author_id=None,
            part_type="state_change",
            meta={"from": prev, "to": "open", "reason": "inbound_reply"},
        )
        await outbox.emit(
            session,
            aggregate=events.AGGREGATE_CONVERSATION,
            aggregate_id=conv.id,
            topic=events.CONVERSATION_STATE_CHANGED,
            payload={**_conversation_payload(conv), "from": prev, "to": "open"},
        )
        if prev == "closed":
            await _on_reopened(session, conv)
    return await _append_part(
        session,
        conv,
        author_kind="contact",
        author_id=conv.contact_id,
        part_type="comment",
        body=body,
        attachments=attachments,
        channel_meta=channel_meta,
    )


async def conversation_contact_id(
    session: AsyncSession, conversation_id: uuid.UUID
) -> uuid.UUID | None:
    """Channel-facing (P0.7): the contact that owns a conversation, or ``None`` if it isn't in the
    caller's workspace (RLS). Used by the email adapter to authenticate an inbound reply's sender
    against the thread's contact before appending."""
    contact_id: uuid.UUID | None = await session.scalar(
        select(Conversation.contact_id).where(Conversation.id == conversation_id)
    )
    return contact_id


@dataclass(frozen=True)
class OutboundEmailPart:
    """A part + its conversation head, flattened for a channel adapter to deliver (P0.7)."""

    conversation_id: uuid.UUID
    contact_id: uuid.UUID
    channel: str
    channel_account_id: uuid.UUID | None
    part_id: uuid.UUID
    author_kind: str
    part_type: str
    body: str | None
    attachments: list[dict[str, Any]]


async def get_outbound_part(
    session: AsyncSession, conversation_id: uuid.UUID, part_id: uuid.UUID
) -> OutboundEmailPart | None:
    """Channel-facing (P0.7): fetch a part + its conversation head for outbound delivery.

    Returns ``None`` when the part isn't in the caller's workspace (RLS). Filters on
    ``(conversation_id, id)`` so the ``parts_thread`` index serves it. Lets the email adapter render
    an agent reply without importing messaging internals (boundary rule)."""
    row = (
        await session.execute(
            select(ConversationPart, Conversation)
            .join(Conversation, Conversation.id == ConversationPart.conversation_id)
            .where(
                ConversationPart.conversation_id == conversation_id,
                ConversationPart.id == part_id,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    part, conv = row
    return OutboundEmailPart(
        conversation_id=conv.id,
        contact_id=conv.contact_id,
        channel=conv.channel,
        channel_account_id=conv.channel_account_id,
        part_id=part.id,
        author_kind=part.author_kind,
        part_type=part.part_type,
        body=part.body,
        attachments=list(part.attachments),
    )


# --- AI adapters: system-initiated Neko turns (no admin Principal, P1.2) ------
# The sanctioned entry points for the ``ai`` module (import-linter allows ai -> messaging.service).
# A Neko turn has no acting admin, so — like the channel adapters above — these take an explicit
# ``conversation_id`` (the worker set the RLS GUC) and run the SAME W1 as agent/contact writes. The
# ai module never re-implements W1 or touches messaging internals; messaging owns the ``ai_agent``
# author kind and the ``ai_status`` field (RFC-002 §5.3), ai owns the orchestration (RFC-003).

_VALID_AI_STATUS = frozenset({"active", "resolved", "handed_off"})


@dataclass(frozen=True)
class AiTurnPart:
    author_kind: str
    part_type: str
    body: str | None
    created_at: dt.datetime


@dataclass(frozen=True)
class AiTurnContext:
    """A read-only snapshot of a conversation for a Neko turn (head + recent parts). Returned to the
    ai module so it never imports messaging models to build a prompt / summary / sentiment."""

    conversation_id: uuid.UUID
    contact_id: uuid.UUID
    channel: str
    state: str
    ai_status: str | None
    recent: list[AiTurnPart]


async def ai_turn_context(
    session: AsyncSession, conversation_id: uuid.UUID, *, history_limit: int = 20
) -> AiTurnContext | None:
    """Head + recent parts (oldest→newest) for a turn, or ``None`` if the conversation isn't in the
    caller's workspace (RLS). Reads the head only for the fields; parts for prompt context."""
    conv = await session.get(Conversation, conversation_id)
    if conv is None:
        return None
    rows = list(
        (
            await session.scalars(
                select(ConversationPart)
                .where(ConversationPart.conversation_id == conversation_id)
                .order_by(ConversationPart.id.desc())
                .limit(history_limit)
            )
        ).all()
    )
    recent = [AiTurnPart(p.author_kind, p.part_type, p.body, p.created_at) for p in reversed(rows)]
    return AiTurnContext(
        conversation_id=conv.id,
        contact_id=conv.contact_id,
        channel=conv.channel,
        state=conv.state,
        ai_status=conv.ai_status,
        recent=recent,
    )


async def append_ai_reply(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    body: str,
    meta: dict[str, Any] | None = None,
) -> ConversationPart:
    """Neko's public answer — an ``ai_agent`` ``comment`` (W1). Clears ``waiting_since`` (Neko
    answered), emits ``conversation.part.created`` for fan-out just like a human reply."""
    conv = await _load_for_update(session, conversation_id)
    return await _append_part(
        session,
        conv,
        author_kind="ai_agent",
        author_id=None,
        part_type="comment",
        body=body,
        meta=meta or {},
    )


async def append_ai_note(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    body: str,
    meta: dict[str, Any] | None = None,
) -> ConversationPart:
    """A private ``ai_agent`` ``note`` (internal only) — the handoff recap so a human starts warm
    (RFC-003 §5). Does not touch ``waiting_since`` (a note is not a reply)."""
    conv = await _load_for_update(session, conversation_id)
    return await _append_part(
        session,
        conv,
        author_kind="ai_agent",
        author_id=None,
        part_type="note",
        body=body,
        meta=meta or {},
    )


async def set_ai_status(session: AsyncSession, *, conversation_id: uuid.UUID, status: str) -> None:
    """Flip ``conversations.ai_status`` (null|active|resolved|handed_off) + emit an outbox event.
    No-op if unchanged. RLS scopes the load to the workspace."""
    if status not in _VALID_AI_STATUS:
        raise ValidationError(f"invalid ai_status {status!r}")
    conv = await _load_for_update(session, conversation_id)
    prev = conv.ai_status
    if prev == status:
        return
    conv.ai_status = status
    await session.flush()
    await outbox.emit(
        session,
        aggregate=events.AGGREGATE_CONVERSATION,
        aggregate_id=conv.id,
        topic=events.CONVERSATION_AI_STATUS_CHANGED,
        payload={**_conversation_payload(conv), "ai_status": status, "prev_ai_status": prev},
    )


# --- Neko resolution surface (P1.3, RFC-003 §8) -------------------------------
# messaging owns conversation state + the part ledger; the ai module owns the resolution
# *definition* (RFC-003 §8). So messaging exposes the raw facts (below) + a system-authored close,
# and ai applies the policy. The claw-back on reopen is triggered from the state-machine here (it
# must ride the reopen txn — master rule 2), delegating the "was it a metered resolution?" decision
# to ai/billing via a lazy import (ai↔messaging only ever touch through the service interface).


@dataclass(frozen=True)
class ResolutionFacts:
    """Everything RFC-003 §8 needs to judge a Neko resolution, read from the conversation head +
    part ledger (so ai never imports messaging models). ``human_replied_after_neko`` is the
    "no human teammate replied after Neko's last answer" clause; ``last_close_*`` identify the most
    recent close (its part id is the meter's ``source_id``; its time bounds the 72 h claw-back)."""

    state: str
    ai_status: str | None
    last_neko_answer_at: dt.datetime | None
    human_replied_after_neko: bool
    last_close_part_id: uuid.UUID | None
    last_close_at: dt.datetime | None


async def resolution_facts(
    session: AsyncSession, conversation_id: uuid.UUID
) -> ResolutionFacts | None:
    """Facts for the RFC-003 §8 resolution test, or ``None`` if the conversation isn't the caller's
    (RLS). Newest-first via the time-ordered uuid7 PK (same ordering as ``ai_turn_context``)."""
    conv = await session.get(Conversation, conversation_id)
    if conv is None:
        return None
    last_neko_answer_at = await session.scalar(
        select(ConversationPart.created_at)
        .where(
            ConversationPart.conversation_id == conversation_id,
            ConversationPart.author_kind == "ai_agent",
            ConversationPart.part_type == "comment",
        )
        .order_by(ConversationPart.id.desc())
        .limit(1)
    )
    human_after = False
    if last_neko_answer_at is not None:
        human_after = (
            await session.scalar(
                select(ConversationPart.id)
                .where(
                    ConversationPart.conversation_id == conversation_id,
                    ConversationPart.author_kind == "admin",
                    ConversationPart.part_type == "comment",
                    ConversationPart.created_at > last_neko_answer_at,
                )
                .limit(1)
            )
        ) is not None
    close_row = (
        await session.execute(
            select(ConversationPart.id, ConversationPart.created_at)
            .where(
                ConversationPart.conversation_id == conversation_id,
                ConversationPart.part_type == "state_change",
                ConversationPart.meta["to"].astext == "closed",
            )
            .order_by(ConversationPart.id.desc())
            .limit(1)
        )
    ).first()
    return ResolutionFacts(
        state=conv.state,
        ai_status=conv.ai_status,
        last_neko_answer_at=last_neko_answer_at,
        human_replied_after_neko=human_after,
        last_close_part_id=close_row[0] if close_row else None,
        last_close_at=close_row[1] if close_row else None,
    )


async def neko_silence_due(
    session: AsyncSession, cutoff: dt.datetime, *, limit: int = 5000
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """(workspace_id, conversation_id) for open, Neko-handling conversations idle since ``cutoff`` —
    the 72 h silence-resolution candidates (RFC-003 §8), across ALL tenants. Uses the
    ``messaging_neko_silence_due`` SECURITY DEFINER function (mirrors the channels/identity pre-
    tenancy resolvers) so the beat sweep runs without a per-workspace GUC. ``limit`` bounds one
    sweep; the caller logs when it's saturated (the next sweep picks up the rest)."""
    rows = await session.execute(
        sa.text(
            "SELECT workspace_id, conversation_id "
            "FROM messaging_neko_silence_due(:cutoff) LIMIT :limit"
        ),
        {"cutoff": cutoff, "limit": limit},
    )
    return [(r[0], r[1]) for r in rows.all()]


async def close_for_resolution(
    session: AsyncSession, conversation_id: uuid.UUID, *, reason: str
) -> uuid.UUID | None:
    """Close a conversation as resolved by Neko (RFC-003 §8) — system-authored, through the state
    machine so the reporting spine + reopen path see it exactly like any other close. Returns the
    closing ``state_change`` part id (the meter's ``source_id``), or ``None`` if already closed
    (idempotent: a re-run doesn't append a second close)."""
    conv = await _load_for_update(session, conversation_id)
    if conv.state == "closed":
        return None
    prev = conv.state
    conv.state = "closed"
    conv.snoozed_until = None
    conv.waiting_since = None
    part = await _append_part(
        session,
        conv,
        author_kind="system",
        author_id=None,
        part_type="state_change",
        meta={"from": prev, "to": "closed", "reason": reason},
    )
    await outbox.emit(
        session,
        aggregate=events.AGGREGATE_CONVERSATION,
        aggregate_id=conv.id,
        topic=events.CONVERSATION_STATE_CHANGED,
        payload={**_conversation_payload(conv), "from": prev, "to": "closed"},
    )
    return part.id


_UNSET: Any = object()  # sentinel: "argument not supplied" (distinct from a real ``None`` ts)


def _encode_cursor(c: Conversation, key: dt.datetime | None = _UNSET) -> str:
    """Encode a keyset cursor as ``<iso-ts>|<public-id>``. ``key`` defaults to ``waiting_since``
    (the R1 order key); pass ``last_part_at`` for the contact-scoped ordering."""
    ts_value = c.waiting_since if key is _UNSET else key
    ts = ts_value.isoformat() if ts_value is not None else ""
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
    unassigned: bool = False,
    cursor: str | None = None,
    limit: int | None = None,
) -> Page[schemas.ConversationOut]:
    """R1 inbox view — open conversations for a team/assignee, ordered by ``waiting_since``.

    The base query (no cursor) is exactly the RFC-002 §6 R1 shape and is served by the partial
    index ``conv_open_team`` / ``conv_open_asgn`` (Index Scan, no Sort — proven by an EXPLAIN
    test). Keyset pagination adds ``id`` as a tiebreak and walks NULL-``waiting_since`` rows last.

    ``unassigned=True`` powers the P0.5 "Unassigned" view (``assignee_id IS NULL``); it is
    mutually exclusive with ``assignee_id`` (a conversation cannot be both).
    """
    n = clamp_limit(limit)
    stmt = select(Conversation).where(Conversation.state == state)
    if team_id is not None:
        stmt = stmt.where(Conversation.team_id == _decode_or_404(IdPrefix.TEAM, team_id, "team"))
    if unassigned:
        stmt = stmt.where(Conversation.assignee_id.is_(None))
    elif assignee_id is not None:
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


async def list_conversations_for_contact(
    session: AsyncSession,
    contact_public_id: str,
    *,
    cursor: str | None = None,
    limit: int | None = None,
) -> Page[schemas.ConversationOut]:
    """A contact's recent conversations across all states — the P0.5 contact side panel.

    Ordered newest-activity-first (``last_part_at`` desc, ``id`` desc tiebreak); keyset
    paginated. RLS scopes rows to the caller's workspace, so a bad contact id simply yields an
    empty page rather than leaking another tenant's threads.
    """
    # Verify the contact belongs to the caller's workspace via the crm service (the only
    # sanctioned cross-module channel, RFC-001 §6.2) so an unknown/other-tenant id 404s rather
    # than silently returning an empty page.
    from relay.modules.crm import service as crm_service

    n = clamp_limit(limit)
    await crm_service.get_contact(session, contact_public_id)  # 404 if not in this workspace
    cid = _decode_or_404(IdPrefix.CONTACT, contact_public_id, "contact")
    stmt = select(Conversation).where(Conversation.contact_id == cid)
    stmt = stmt.order_by(Conversation.last_part_at.desc(), Conversation.id.desc())
    if cursor:
        lpa, i = _decode_cursor(cursor)
        if lpa is not None:
            stmt = stmt.where(
                sa.or_(
                    Conversation.last_part_at < lpa,
                    sa.and_(Conversation.last_part_at == lpa, Conversation.id < i),
                )
            )
    convs = list((await session.scalars(stmt.limit(n + 1))).all())
    next_cursor = None
    if len(convs) > n:
        convs = convs[:n]
        last = convs[-1]
        next_cursor = _encode_cursor(last, key=last.last_part_at)
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


async def list_parts_after(
    session: AsyncSession, public_id: str, *, after: str | None = None, limit: int | None = None
) -> Page[schemas.PartOut]:
    """Ascending parts newer than ``after`` — the realtime long-poll fallback (RFC-001 §6.3).

    When the websocket is down, clients poll this with the id of the newest part they hold; the
    outbox/DB is the source of truth, so a gateway outage never loses a message (parts are read
    straight from Postgres). UUIDv7 part ids are time-ordered, so the ``id > after`` keyset is
    chronological. Gated by the ``realtime_fallback`` kill switch."""
    if not get_settings().realtime_fallback:
        raise PermissionDeniedError("realtime fallback polling is disabled")
    cid = _decode_or_404(IdPrefix.CONVERSATION, public_id, "conversation")
    await _get(session, cid)  # 404 (RLS-scoped) if the conversation isn't this workspace's
    n = clamp_limit(limit)
    stmt = select(ConversationPart).where(ConversationPart.conversation_id == cid)
    if after:
        stmt = stmt.where(ConversationPart.id > _decode_or_404(IdPrefix.PART, after, "after"))
    stmt = stmt.order_by(ConversationPart.id.asc()).limit(n + 1)
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
    if target == "open" and prev == "closed":
        await _on_reopened(session, conv)
    return conversation_out(conv)


async def _on_reopened(session: AsyncSession, conv: Conversation) -> None:
    """A closed conversation was reopened — let the ai module claw back a Neko resolution meter if
    this close was one and it's still inside the 72 h window (RFC-003 §8, same reopen txn — master
    rule 2). Lazy import: ai↔messaging touch only through the service interface (import-linter), and
    a lazy import sidesteps the module-load cycle (ai.pipeline imports messaging.service)."""
    from relay.modules.ai import service as ai_service

    await ai_service.on_conversation_reopened(
        session, workspace_id=conv.workspace_id, conversation_id=conv.id
    )


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


# --- Realtime: tokens, subscription authz, typing, presence (RFC-001 §6.3) ----


def realtime_token(principal: Principal) -> schemas.RealtimeTokenOut:
    """Mint an agent's Centrifugo connection token (identity only; channels are authorised
    per-subscription below)."""
    authorize(principal, min_role=Role.AGENT)
    token = realtime.agent_connection_token(
        admin_id=principal.admin_id, workspace_id=principal.workspace_id, role=principal.role
    )
    return schemas.RealtimeTokenOut(token=token, ws_url=get_settings().centrifugo_ws_url)


async def _authorize_channel(session: AsyncSession, principal: Principal, channel: str) -> None:
    """Reject any channel the agent may not subscribe to. ``conv:*`` is validated by loading the
    conversation (RLS scopes it to the workspace → 404 for another tenant's conversation);
    ``inbox:{ws}:{team}`` must carry the caller's own workspace id."""
    if channel.startswith("conv:"):
        await _get(
            session, _decode_or_404(IdPrefix.CONVERSATION, channel[len("conv:") :], "channel")
        )
    elif channel.startswith("inbox:"):
        segments = channel.split(":")
        own_ws = encode_public_id(IdPrefix.WORKSPACE, principal.workspace_id)
        if len(segments) != 3 or segments[1] != own_ws:
            raise PermissionDeniedError("channel is not in your workspace")
    else:
        raise ValidationError(f"unknown channel {channel!r}")


async def realtime_subscribe(
    session: AsyncSession, principal: Principal, req: schemas.SubscribeIn
) -> schemas.SubscribeOut:
    """Mint per-channel subscription tokens for the agent, one per authorised channel."""
    authorize(principal, min_role=Role.AGENT)
    sub = str(principal.admin_id)
    tokens: dict[str, str] = {}
    for channel in req.channels:
        await _authorize_channel(session, principal, channel)
        tokens[channel] = realtime.mint_subscription_token(sub, channel)
    return schemas.SubscribeOut(tokens=tokens, ws_url=get_settings().centrifugo_ws_url)


def widget_token(conv: Conversation) -> str:
    """Mint a widget contact's connection token, pinned to its one conversation channel.

    ponytail: exposed as a service function for P0.4 (proven by test); the widget HTTP surface
    that authenticates a contact and calls this lands in P0.6 (identity verification)."""
    return realtime.widget_connection_token(
        workspace_id=conv.workspace_id, contact_id=conv.contact_id, conversation_id=conv.id
    )


async def relay_typing(session: AsyncSession, principal: Principal, public_id: str) -> None:
    """Relay an agent typing indicator to the conversation channel (Redis TTL + Centrifugo)."""
    authorize(principal, min_role=Role.AGENT)
    cid = _decode_or_404(IdPrefix.CONVERSATION, public_id, "conversation")
    await _get(session, cid)  # 404 if not this workspace's conversation
    await realtime.relay_typing(
        encode_public_id(IdPrefix.CONVERSATION, cid),
        actor_kind="admin",
        actor_id=encode_public_id(IdPrefix.ADMIN, principal.admin_id),
    )


async def presence_heartbeat(principal: Principal) -> None:
    """Mark the agent online (Redis TTL) and relay presence to the workspace inbox firehose."""
    authorize(principal, min_role=Role.AGENT)
    await realtime.mark_online(
        encode_public_id(IdPrefix.WORKSPACE, principal.workspace_id),
        encode_public_id(IdPrefix.ADMIN, principal.admin_id),
    )


# --- Widget (messenger) BFF ---------------------------------------------------
#
# The end-user surface for the P0.6 messenger. It composes identity (workspace settings +
# HMAC identity verification), crm (contact/lead resolution) and messaging (conversations)
# behind one contact-scoped API. Everything below runs as a ``ContactPrincipal``: no RBAC role,
# so the ``authorize`` choke point rejects it from every agent path; a contact only ever touches
# its own conversations (checked here) and RLS scopes reads to the workspace.


def _messenger_config(ws: identity_service.WidgetSettings) -> schemas.MessengerConfig:
    """Project the raw ``settings['messenger']`` blob into the public, secret-free config the
    widget themes from. Missing/garbage keys fall back to safe defaults."""
    m = ws.messenger

    def _obj(key: str) -> dict[str, Any]:
        val = m.get(key)
        return val if isinstance(val, dict) else {}

    theme = _obj("theme")
    iv = _obj("identity_verification")
    raw_hours = m.get("office_hours")
    office_hours = raw_hours if isinstance(raw_hours, dict) else None
    position = theme.get("launcher_position")
    return schemas.MessengerConfig(
        primary_color=theme.get("primary_color") or "#2563eb",
        launcher_position=position if position in ("left", "right") else "right",
        greeting=m.get("greeting"),
        expected_reply_time=m.get("expected_reply_time"),
        office_hours=office_hours,
        identity_verification_enabled=bool(iv.get("enabled")),
    )


def _identity_secret(ws: identity_service.WidgetSettings) -> str | None:
    iv = ws.messenger.get("identity_verification")
    return iv.get("secret") if isinstance(iv, dict) else None


def _resume_contact_id(token: str | None, workspace_id: uuid.UUID) -> uuid.UUID | None:
    """Decode a reload's session token (cookie or resume_token) to the lead it represents, but
    only if it belongs to *this* workspace — a token from another tenant never resumes here."""
    if not token:
        return None
    try:
        claims = decode_widget_session_token(token)
        if uuid.UUID(claims["ws"]) != workspace_id:
            return None
        return uuid.UUID(claims["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        return None


async def _contact_conversations(
    session: AsyncSession, contact_id: uuid.UUID
) -> list[schemas.ConversationOut]:
    stmt = (
        select(Conversation)
        .where(Conversation.contact_id == contact_id)
        .order_by(Conversation.id.desc())  # uuid7 → newest first
        .limit(clamp_limit(None))
    )
    return [conversation_out(c) for c in (await session.scalars(stmt)).all()]


async def widget_boot(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    req: schemas.WidgetBootRequest,
    cookie_token: str | None,
) -> schemas.WidgetBootResponse:
    """Boot a messenger session.

    Identity verification ON → the host page must supply ``user.external_id`` + a matching
    ``user_hash`` (HMAC-SHA256(secret, external_id)); a mismatch is rejected (403). OFF → resume
    the cookie's lead or create a fresh cookie-scoped one. Returns a widget session token (also
    set as an httpOnly cookie by the router), the public config, and the contact's conversations.
    """
    ws = await identity_service.widget_settings(session, workspace_id)  # 404 for unknown app_id
    config = _messenger_config(ws)
    user = req.user

    if config.identity_verification_enabled:
        if not user or not user.external_id or not req.user_hash:
            raise PermissionDeniedError("identity verification is required for this workspace")
        secret = _identity_secret(ws)
        if not secret or not verify_identity_hash(secret, user.external_id, req.user_hash):
            raise PermissionDeniedError("identity verification failed")
        contact = await crm_service.resolve_widget_contact(
            session,
            workspace_id=workspace_id,
            verified=True,
            external_id=user.external_id,
            email=user.email,
            name=user.name,
        )
    else:
        resume = _resume_contact_id(cookie_token or req.resume_token, workspace_id)
        contact = await crm_service.resolve_widget_contact(
            session,
            workspace_id=workspace_id,
            verified=False,
            email=user.email if user else None,
            name=user.name if user else None,
            cookie_contact_id=resume,
        )

    contact_uuid = decode_public_id(IdPrefix.CONTACT, contact.id)
    token = create_widget_session_token(contact_id=contact_uuid, workspace_id=workspace_id)
    return schemas.WidgetBootResponse(
        session_token=token,
        contact=schemas.WidgetContactOut(
            id=contact.id, kind=contact.kind, email=contact.email, name=contact.name
        ),
        config=config,
        conversations=await _contact_conversations(session, contact_uuid),
    )


async def _load_contact_conv(
    session: AsyncSession, public_id: str, contact_id: uuid.UUID, *, for_update: bool = False
) -> Conversation:
    """Load a conversation and assert it belongs to this contact. Any other conversation 404s
    (never leak that another visitor's conversation exists), even within the same workspace."""
    cid = _decode_or_404(IdPrefix.CONVERSATION, public_id, "conversation")
    conv = await (_load_for_update(session, cid) if for_update else _get(session, cid))
    if conv.contact_id != contact_id:
        raise NotFoundError("conversation not found")
    return conv


async def contact_start_conversation(
    session: AsyncSession, contact: ContactPrincipal, req: schemas.WidgetStartConversation
) -> schemas.ConversationOut:
    """A visitor opens a new conversation with their first message (RFC-001 §6.3 heartbeat)."""
    conv = await _open_conversation(
        session,
        workspace_id=contact.workspace_id,
        contact_id=contact.contact_id,
        channel="chat",
        team_id=None,
        body=req.body,
        attachments=req.attachments,
    )
    return conversation_out(conv)


async def contact_reply(
    session: AsyncSession, contact: ContactPrincipal, public_id: str, req: schemas.WidgetReplyIn
) -> schemas.PartOut:
    """A visitor's follow-up message — a contact ``comment`` (starts the SLA clock via W1)."""
    conv = await _load_contact_conv(session, public_id, contact.contact_id, for_update=True)
    part = await _append_part(
        session,
        conv,
        author_kind="contact",
        author_id=contact.contact_id,
        part_type="comment",
        body=req.body,
        attachments=req.attachments,
    )
    return part_out(part)


async def contact_list_conversations(
    session: AsyncSession,
    contact: ContactPrincipal,
    *,
    cursor: str | None = None,
    limit: int | None = None,
) -> Page[schemas.ConversationOut]:
    """The visitor's own conversation list (newest first, keyset)."""
    n = clamp_limit(limit)
    stmt = select(Conversation).where(Conversation.contact_id == contact.contact_id)
    if cursor:
        stmt = stmt.where(Conversation.id < _decode_or_404(IdPrefix.CONVERSATION, cursor, "cursor"))
    convs = list((await session.scalars(stmt.order_by(Conversation.id.desc()).limit(n + 1))).all())
    next_cursor = None
    if len(convs) > n:
        convs = convs[:n]
        next_cursor = encode_public_id(IdPrefix.CONVERSATION, convs[-1].id)
    return Page(items=[conversation_out(c) for c in convs], next_cursor=next_cursor)


async def contact_list_parts(
    session: AsyncSession,
    contact: ContactPrincipal,
    public_id: str,
    *,
    after: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
) -> Page[schemas.PartOut]:
    """Thread page for a conversation the contact owns. ``?after=`` is the realtime long-poll
    fallback (ascending); otherwise the newest-first page. Ownership is checked first, then the
    agent read path is reused verbatim."""
    await _load_contact_conv(session, public_id, contact.contact_id)  # 404 unless owned
    if after is not None:
        return await list_parts_after(session, public_id, after=after, limit=limit)
    return await list_parts(session, public_id, cursor=cursor, limit=limit)


async def contact_rate(
    session: AsyncSession, contact: ContactPrincipal, public_id: str, req: schemas.WidgetRatingIn
) -> schemas.PartOut:
    """A conversation rating (CSAT) submitted by the visitor, typically on close."""
    conv = await _load_contact_conv(session, public_id, contact.contact_id, for_update=True)
    part = await _append_part(
        session,
        conv,
        author_kind="contact",
        author_id=contact.contact_id,
        part_type="rating",
        body=req.remark,
        meta={"rating": req.rating},
    )
    return part_out(part)


async def contact_confirm_resolution(
    session: AsyncSession, contact: ContactPrincipal, public_id: str
) -> schemas.ConversationOut:
    """The customer confirms Neko resolved their question (RFC-003 §8 "confirmed resolution"). The
    ai module decides if it qualifies as a metered resolution and, if so, closes it + meters — all
    in this one txn. Idempotent: a second confirm on an already-closed conversation is a no-op."""
    conv = await _load_contact_conv(session, public_id, contact.contact_id, for_update=True)
    from relay.modules.ai import service as ai_service  # lazy: avoid the ai↔messaging load cycle

    await ai_service.confirm_resolution(
        session, workspace_id=conv.workspace_id, conversation_id=conv.id
    )
    return conversation_out(conv)


async def contact_typing(session: AsyncSession, contact: ContactPrincipal, public_id: str) -> None:
    """Relay a visitor typing indicator to the conversation channel (Redis TTL + Centrifugo)."""
    conv = await _load_contact_conv(session, public_id, contact.contact_id)
    await realtime.relay_typing(
        encode_public_id(IdPrefix.CONVERSATION, conv.id),
        actor_kind="contact",
        actor_id=encode_public_id(IdPrefix.CONTACT, contact.contact_id),
    )


async def contact_realtime_token(
    session: AsyncSession, contact: ContactPrincipal, public_id: str
) -> schemas.RealtimeTokenOut:
    """Mint the widget's Centrifugo connection token, pinned by the gateway to exactly this
    conversation's channel (RFC-001 §6.3; the pinning is proven by the P0.4 token test)."""
    conv = await _load_contact_conv(session, public_id, contact.contact_id)
    token = realtime.widget_connection_token(
        workspace_id=conv.workspace_id, contact_id=conv.contact_id, conversation_id=conv.id
    )
    return schemas.RealtimeTokenOut(token=token, ws_url=get_settings().centrifugo_ws_url)


# --- Cross-module read: queue snapshot for the reporting queue monitor (P0.9) -------------------


async def queue_snapshot(session: AsyncSession) -> dict[str, int | None]:
    """Live inbox counts for the P0.9 queue monitor, computed from the conversation **head** only
    (never ``conversation_parts``). RLS scopes every count to the caller's workspace. Exposed on the
    service boundary so ``reporting`` reads these without importing messaging's models."""
    open_count = (
        await session.execute(
            select(sa.func.count()).select_from(Conversation).where(Conversation.state == "open")
        )
    ).scalar_one()
    unassigned = (
        await session.execute(
            select(sa.func.count())
            .select_from(Conversation)
            .where(Conversation.state == "open", Conversation.assignee_id.is_(None))
        )
    ).scalar_one()
    oldest_waiting = (
        await session.execute(
            select(sa.func.min(Conversation.waiting_since)).where(Conversation.state == "open")
        )
    ).scalar_one()
    longest_wait_s = (
        int((_now() - oldest_waiting).total_seconds()) if oldest_waiting is not None else None
    )
    return {"open": open_count, "unassigned": unassigned, "longest_wait_s": longest_wait_s}
