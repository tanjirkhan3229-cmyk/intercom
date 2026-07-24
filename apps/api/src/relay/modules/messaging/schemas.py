"""Pydantic request/response models for the ``messaging`` API. IDs are prefixed base62 strings."""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field, model_validator

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


# --- Widget (messenger) BFF ---------------------------------------------------


class WidgetBootUser(BaseModel):
    """Optional identity the host page supplies. ``external_id`` is the tenant's own user id;
    with identity verification on it must be accompanied by a valid ``user_hash``."""

    external_id: str | None = None
    email: str | None = None
    name: str | None = None


class WidgetBootRequest(BaseModel):
    app_id: str  # public workspace id (wrk_...) — safe to embed on any customer page
    user: WidgetBootUser | None = None
    user_hash: str | None = None
    # Reload continuity when third-party cookies are blocked: the iframe re-sends the session
    # token it stored. The httpOnly cookie is the primary mechanism; this is the fallback.
    resume_token: str | None = None


class MessengerConfig(BaseModel):
    """Public messenger config the widget themes itself from (no secrets)."""

    primary_color: str
    launcher_position: str  # "left" | "right"
    greeting: str | None = None
    expected_reply_time: str | None = None  # office-hours model lands in P1.7; free-form for now
    office_hours: dict[str, Any] | None = None
    identity_verification_enabled: bool


class WidgetContactOut(BaseModel):
    id: str
    kind: str  # "user" (verified) | "lead" (anonymous)
    email: str | None = None
    name: str | None = None


class WidgetBootResponse(BaseModel):
    session_token: str
    contact: WidgetContactOut
    config: MessengerConfig
    conversations: list[ConversationOut]
    # Delivered-but-unseen in-app posts (P1.8), caught up on boot. Untyped dicts avoid a
    # cross-module schema import (the outbound module owns the shape).
    posts: list[dict[str, Any]] = Field(default_factory=list)


class WidgetStartConversation(BaseModel):
    body: str = Field(min_length=1)
    attachments: list[dict[str, Any]] = Field(default_factory=list)


class WidgetReplyIn(BaseModel):
    body: str = Field(min_length=1)
    attachments: list[dict[str, Any]] = Field(default_factory=list)


class WidgetRatingIn(BaseModel):
    rating: int = Field(ge=1, le=5)
    remark: str | None = None


# --- P1.7 Office hours --------------------------------------------------------


class OfficeHoursInterval(BaseModel):
    """A single open span within a local day, ``HH:MM`` (00:00-24:00)."""

    open: str = Field(pattern=r"^([01]\d|2[0-4]):([0-5]\d)$")
    close: str = Field(pattern=r"^([01]\d|2[0-4]):([0-5]\d)$")


class OfficeHoursScheduleIn(BaseModel):
    """Create/replace a schedule. ``team_id`` omitted ⇒ the workspace default. ``weekly`` maps a
    weekday ``"0".."6"`` (Mon=0) to its open intervals; empty ⇒ closed that day."""

    team_id: str | None = None
    timezone: str = Field(min_length=1, max_length=64)
    weekly: dict[str, list[OfficeHoursInterval]] = Field(default_factory=dict)
    holidays: list[str] = Field(default_factory=list)


class OfficeHoursScheduleOut(BaseModel):
    id: str
    team_id: str | None
    timezone: str
    weekly: dict[str, list[OfficeHoursInterval]]
    holidays: list[str]
    created_at: dt.datetime
    updated_at: dt.datetime


class OfficeHoursStatusOut(BaseModel):
    """Whether the resolved schedule (team override → workspace default) is open right now."""

    has_schedule: bool
    is_open: bool
    timezone: str | None = None


# --- P1.7 SLA -----------------------------------------------------------------


class SlaEscalation(BaseModel):
    """Breach actions. ``reassign_team_id`` routes the conversation to a team (clearing assignee);
    ``set_priority`` flags it; ``notify`` posts a system note on the thread."""

    set_priority: bool = False
    notify: bool = False
    reassign_team_id: str | None = None


class SlaPolicyIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    active: bool = True
    first_response_seconds: int | None = Field(default=None, ge=1)
    next_response_seconds: int | None = Field(default=None, ge=1)
    resolution_seconds: int | None = Field(default=None, ge=1)
    business_hours: bool = False
    # A predicates AST (validated server-side); when set the policy auto-applies to matching new
    # conversations. ``None`` ⇒ apply only manually or via the workflow ``apply_sla`` action.
    apply_predicate: dict[str, Any] | None = None
    escalation: SlaEscalation = Field(default_factory=SlaEscalation)
    position: int = 0

    @model_validator(mode="after")
    def _at_least_one_target(self) -> SlaPolicyIn:
        if (
            self.first_response_seconds is None
            and self.next_response_seconds is None
            and self.resolution_seconds is None
        ):
            raise ValueError("an SLA policy must set at least one target")
        return self


class SlaPolicyOut(BaseModel):
    id: str
    name: str
    active: bool
    first_response_seconds: int | None
    next_response_seconds: int | None
    resolution_seconds: int | None
    business_hours: bool
    apply_predicate: dict[str, Any] | None
    escalation: SlaEscalation
    position: int
    created_at: dt.datetime
    updated_at: dt.datetime


class ApplySlaIn(BaseModel):
    policy_id: str


class SlaTargetState(BaseModel):
    due_at: dt.datetime | None = None
    satisfied_at: dt.datetime | None = None
    breached_at: dt.datetime | None = None


class ConversationSlaOut(BaseModel):
    conversation_id: str
    policy_id: str
    applied_at: dt.datetime
    first_response: SlaTargetState
    next_response: SlaTargetState
    resolution: SlaTargetState
    next_breach_at: dt.datetime | None
    active: bool


# --- P1.7 Custom inbox views --------------------------------------------------


class InboxViewIn(BaseModel):
    """A saved filter. ``filter`` is a predicates AST over conversation fields (validated on save);
    ``team_id`` shares the view with a team (omitted ⇒ personal/workspace)."""

    name: str = Field(min_length=1, max_length=255)
    filter: dict[str, Any] = Field(default_factory=dict)
    team_id: str | None = None


class InboxViewOut(BaseModel):
    id: str
    name: str
    filter: dict[str, Any]
    team_id: str | None
    created_at: dt.datetime
    updated_at: dt.datetime


class ViewCountOut(BaseModel):
    count: int


# --- P1.7 Balanced assignment (agent availability) ----------------------------


class AgentAvailabilityIn(BaseModel):
    away: bool = False
    max_open: int | None = Field(default=None, ge=0)


class AgentAvailabilityOut(BaseModel):
    admin_id: str
    away: bool
    max_open: int | None
    updated_at: dt.datetime


class BalancedAssignIn(BaseModel):
    team_id: str


# --- P1.7 Collision detection (who's viewing / typing) ------------------------


class PresenceActor(BaseModel):
    actor_kind: str  # "admin" | "contact"
    actor_id: str


class ConversationPresenceOut(BaseModel):
    """Live collision state for a conversation: who has it open + who is typing."""

    viewers: list[str]  # admin public ids currently viewing
    typers: list[PresenceActor]
