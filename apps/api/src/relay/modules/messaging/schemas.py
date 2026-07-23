"""Pydantic request/response models for the ``messaging`` API. IDs are prefixed base62 strings."""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field

_CHANNEL_PATTERN = "^(chat|email|whatsapp|messenger_fb|instagram|sms|voice|api)$"

# --- Conversations ------------------------------------------------------------


class ConversationCreate(BaseModel):
    """Open a conversation for a contact with their first message (models a visitor message)."""

    contact_id: str
    body: str = Field(min_length=1)
    channel: str = Field(default="chat", pattern=_CHANNEL_PATTERN)
    team_id: str | None = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    channel_meta: dict[str, Any] = Field(default_factory=dict)


class ConversationOut(BaseModel):
    id: str
    contact_id: str
    channel: str
    state: str
    assignee_id: str | None
    team_id: str | None
    priority: bool
    waiting_since: dt.datetime | None
    snoozed_until: dt.datetime | None
    last_part_at: dt.datetime
    first_contact_reply_at: dt.datetime | None
    ai_status: str | None
    created_at: dt.datetime


# --- Parts --------------------------------------------------------------------


class ReplyIn(BaseModel):
    """An agent reply (public ``comment``)."""

    body: str = Field(min_length=1)
    attachments: list[dict[str, Any]] = Field(default_factory=list)


class NoteIn(BaseModel):
    """An internal note (not visible to the contact)."""

    body: str = Field(min_length=1)


class RatingIn(BaseModel):
    rating: int = Field(ge=1, le=5)
    remark: str | None = None


class PartOut(BaseModel):
    id: str
    conversation_id: str
    author_kind: str
    author_id: str | None
    part_type: str
    body: str | None
    attachments: list[dict[str, Any]]
    meta: dict[str, Any]
    created_at: dt.datetime


# --- State + assignment -------------------------------------------------------


class StateChangeIn(BaseModel):
    state: str = Field(pattern="^(open|snoozed|closed)$")
    snoozed_until: dt.datetime | None = None


class AssignIn(BaseModel):
    """Manual assignment. At least one of ``assignee_id`` / ``team_id`` (both allowed)."""

    assignee_id: str | None = None
    team_id: str | None = None


class RoundRobinIn(BaseModel):
    team_id: str


# --- Saved replies (macros) + tags --------------------------------------------


class SavedReplyCreate(BaseModel):
    shortcut: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1)


class SavedReplyOut(BaseModel):
    id: str
    shortcut: str
    title: str
    body: str
    created_at: dt.datetime


class TagIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class TagOut(BaseModel):
    name: str


# --- Realtime (Centrifugo tokens + typing) ------------------------------------


class RealtimeTokenOut(BaseModel):
    """An agent's Centrifugo connection token + the websocket URL to dial."""

    token: str
    ws_url: str


class SubscribeIn(BaseModel):
    """Request per-channel subscription tokens. Each channel is authorised against the caller's
    workspace before a token is minted (``conv:*`` must belong to the workspace; ``inbox:{ws}:*``
    must carry the caller's own workspace id)."""

    channels: list[str] = Field(min_length=1, max_length=100)


class SubscribeOut(BaseModel):
    tokens: dict[str, str]
    ws_url: str


class TypingIn(BaseModel):
    """A no-body typing ping is fine; the actor is the authenticated agent."""

    typing: bool = True
