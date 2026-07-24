"""HTTP routes for the ``messaging`` module (RFC-002 §5.3). Mounted by relay.main under ``/v0``.

Auth/tenancy come from the shared kernel (``relay.core.deps``); RBAC is enforced in the service
layer through the ``authorize`` choke point. Mutating endpoints carry ``@idempotent`` so a
retried request with the same ``Idempotency-Key`` header replays the original response and
creates exactly one row (RFC-002 §7). List endpoints are keyset-paginated.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Request, Response, status

from relay.core.db import session_scope
from relay.core.deps import ContactSession, CurrentPrincipal, SessionDep
from relay.core.errors import NotFoundError
from relay.core.idempotency import idempotent
from relay.core.ids import IdPrefix, decode_public_id
from relay.core.pagination import Page
from relay.settings import get_settings

from . import assignment, office_hours, schemas, service, sla, views

router = APIRouter(tags=["messaging"])

# httpOnly cookie carrying the widget session token, so a lead's session survives a reload
# (RFC-001 §10 acceptance). Cross-site iframe ⇒ SameSite=None; Secure. Path=/v0 so it rides
# every widget API call. ponytail: third-party-cookie-blocked browsers fall back to the
# `resume_token` the iframe stores in its own storage — the cookie is the primary path.
WIDGET_SESSION_COOKIE = "relay_widget"


# --- Conversations ------------------------------------------------------------


@router.post("/conversations", response_model=schemas.ConversationOut, status_code=201)
@idempotent(status_code=201)
async def create_conversation(
    req: schemas.ConversationCreate,
    request: Request,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> schemas.ConversationOut:
    return await service.create_conversation(session, principal, req)


@router.get("/conversations", response_model=Page[schemas.ConversationOut])
async def list_conversations(
    _principal: CurrentPrincipal,
    session: SessionDep,
    state: str = Query(default="open", pattern="^(open|snoozed|closed)$"),
    team_id: str | None = Query(default=None),
    assignee_id: str | None = Query(default=None),
    unassigned: bool = Query(
        default=False, description="Return only conversations with no assignee (Unassigned view)."
    ),
    cursor: str | None = None,
    limit: int | None = Query(default=None, ge=1, le=200),
) -> Page[schemas.ConversationOut]:
    return await service.list_conversations(
        session,
        state=state,
        team_id=team_id,
        assignee_id=assignee_id,
        unassigned=unassigned,
        cursor=cursor,
        limit=limit,
    )


@router.get("/contacts/{contact_id}/conversations", response_model=Page[schemas.ConversationOut])
async def list_contact_conversations(
    contact_id: str,
    _principal: CurrentPrincipal,
    session: SessionDep,
    cursor: str | None = None,
    limit: int | None = Query(default=None, ge=1, le=200),
) -> Page[schemas.ConversationOut]:
    """A contact's recent conversations (all states) for the inbox contact side panel."""
    return await service.list_conversations_for_contact(
        session, contact_id, cursor=cursor, limit=limit
    )


@router.get("/conversations/{conversation_id}", response_model=schemas.ConversationOut)
async def get_conversation(
    conversation_id: str, _principal: CurrentPrincipal, session: SessionDep
) -> schemas.ConversationOut:
    return await service.get_conversation(session, conversation_id)


@router.get("/conversations/{conversation_id}/parts", response_model=Page[schemas.PartOut])
async def list_parts(
    conversation_id: str,
    _principal: CurrentPrincipal,
    session: SessionDep,
    cursor: str | None = None,
    after: str | None = Query(
        default=None,
        description="Realtime long-poll fallback: return parts *newer* than this part id, "
        "ascending (RFC-001 §6.3). Gated by the realtime_fallback flag.",
    ),
    limit: int | None = Query(default=None, ge=1, le=200),
) -> Page[schemas.PartOut]:
    # ?after= is the long-poll fallback (ascending); otherwise the default newest-first thread page.
    if after is not None:
        return await service.list_parts_after(session, conversation_id, after=after, limit=limit)
    return await service.list_parts(session, conversation_id, cursor=cursor, limit=limit)


# --- Parts (W1) ---------------------------------------------------------------


@router.post(
    "/conversations/{conversation_id}/reply", response_model=schemas.PartOut, status_code=201
)
@idempotent(status_code=201)
async def reply(
    conversation_id: str,
    req: schemas.ReplyIn,
    request: Request,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> schemas.PartOut:
    return await service.add_reply(session, principal, conversation_id, req)


@router.post(
    "/conversations/{conversation_id}/notes", response_model=schemas.PartOut, status_code=201
)
@idempotent(status_code=201)
async def add_note(
    conversation_id: str,
    req: schemas.NoteIn,
    request: Request,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> schemas.PartOut:
    return await service.add_note(session, principal, conversation_id, req)


@router.post(
    "/conversations/{conversation_id}/rating", response_model=schemas.PartOut, status_code=201
)
@idempotent(status_code=201)
async def add_rating(
    conversation_id: str,
    req: schemas.RatingIn,
    request: Request,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> schemas.PartOut:
    return await service.add_rating(session, principal, conversation_id, req)


# --- State + assignment (W4) --------------------------------------------------


@router.post("/conversations/{conversation_id}/state", response_model=schemas.ConversationOut)
@idempotent(status_code=200)
async def change_state(
    conversation_id: str,
    req: schemas.StateChangeIn,
    request: Request,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> schemas.ConversationOut:
    return await service.change_state(session, principal, conversation_id, req)


@router.post("/conversations/{conversation_id}/assign", response_model=schemas.ConversationOut)
@idempotent(status_code=200)
async def assign(
    conversation_id: str,
    req: schemas.AssignIn,
    request: Request,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> schemas.ConversationOut:
    return await service.assign(session, principal, conversation_id, req)


@router.post("/conversations/{conversation_id}/claim", response_model=schemas.ConversationOut)
@idempotent(status_code=200)
async def claim(
    conversation_id: str,
    request: Request,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> schemas.ConversationOut:
    return await service.claim(session, principal, conversation_id)


@router.post(
    "/conversations/{conversation_id}/assign/round-robin",
    response_model=schemas.ConversationOut,
)
@idempotent(status_code=200)
async def assign_round_robin(
    conversation_id: str,
    req: schemas.RoundRobinIn,
    request: Request,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> schemas.ConversationOut:
    return await service.assign_round_robin(session, principal, conversation_id, req)


# --- Tags ---------------------------------------------------------------------


@router.get("/conversations/{conversation_id}/tags", response_model=list[schemas.TagOut])
async def list_tags(
    conversation_id: str, _principal: CurrentPrincipal, session: SessionDep
) -> list[schemas.TagOut]:
    return await service.list_tags(session, conversation_id)


@router.post("/conversations/{conversation_id}/tags", status_code=204)
async def add_tag(
    conversation_id: str,
    req: schemas.TagIn,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> Response:
    await service.add_tag(session, principal, conversation_id, req)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/conversations/{conversation_id}/tags/{name}", status_code=204)
async def remove_tag(
    conversation_id: str, name: str, principal: CurrentPrincipal, session: SessionDep
) -> Response:
    await service.remove_tag(session, principal, conversation_id, name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Saved replies (macros) ---------------------------------------------------


@router.get("/saved-replies", response_model=list[schemas.SavedReplyOut])
async def list_saved_replies(
    _principal: CurrentPrincipal, session: SessionDep
) -> list[schemas.SavedReplyOut]:
    return await service.list_saved_replies(session)


@router.post("/saved-replies", response_model=schemas.SavedReplyOut, status_code=201)
@idempotent(status_code=201)
async def create_saved_reply(
    req: schemas.SavedReplyCreate,
    request: Request,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> schemas.SavedReplyOut:
    return await service.create_saved_reply(session, principal, req)


@router.delete("/saved-replies/{reply_id}", status_code=204)
async def delete_saved_reply(
    reply_id: str, principal: CurrentPrincipal, session: SessionDep
) -> Response:
    await service.delete_saved_reply(session, principal, reply_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Realtime (Centrifugo tokens + typing/presence, RFC-001 §6.3) -------------


@router.post("/realtime/token", response_model=schemas.RealtimeTokenOut)
async def realtime_token(principal: CurrentPrincipal) -> schemas.RealtimeTokenOut:
    """Mint the agent's Centrifugo connection token + return the websocket URL to dial."""
    return service.realtime_token(principal)


@router.post("/realtime/subscribe", response_model=schemas.SubscribeOut)
async def realtime_subscribe(
    req: schemas.SubscribeIn, principal: CurrentPrincipal, session: SessionDep
) -> schemas.SubscribeOut:
    """Mint per-channel subscription tokens, each authorised against the caller's workspace."""
    return await service.realtime_subscribe(session, principal, req)


@router.post("/realtime/presence", status_code=204)
async def presence_heartbeat(principal: CurrentPrincipal) -> Response:
    """Refresh the agent's presence (Redis TTL) and relay it through Centrifugo."""
    await service.presence_heartbeat(principal)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/conversations/{conversation_id}/typing", status_code=204)
async def typing(
    conversation_id: str,
    _req: schemas.TypingIn,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> Response:
    """Relay a typing indicator to the conversation channel (ephemeral: Redis TTL + Centrifugo)."""
    await service.relay_typing(session, principal, conversation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/conversations/{conversation_id}/viewing", status_code=204)
async def viewing(
    conversation_id: str, principal: CurrentPrincipal, session: SessionDep
) -> Response:
    """Heartbeat that the agent has this conversation open (collision detection: Redis TTL +
    a relayed ``view`` event)."""
    await service.mark_viewing(session, principal, conversation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/conversations/{conversation_id}/presence", response_model=schemas.ConversationPresenceOut
)
async def conversation_presence(
    conversation_id: str, principal: CurrentPrincipal, session: SessionDep
) -> schemas.ConversationPresenceOut:
    """Who currently has this conversation open + who is typing (the inbox soft-lock warning)."""
    return await service.conversation_presence(session, principal, conversation_id)


# --- Widget (messenger) BFF — end-user/contact surface (RFC-000 §2.1, RFC-001 §6.3) ----------
#
# ``/widget/boot`` is unauthenticated: it resolves the workspace from the public ``app_id`` and
# scopes RLS to it itself (a contact session doesn't exist yet). Every other widget route runs
# as a ``ContactSession`` (a contact/lead JWT) and can only ever touch that contact's own
# conversations. Mutations carry ``@idempotent`` (public retries must not double-send).


def _workspace_from_app_id(app_id: str) -> uuid.UUID:
    try:
        return decode_public_id(IdPrefix.WORKSPACE, app_id)
    except ValueError as exc:
        raise NotFoundError("workspace not found") from exc


@router.post("/widget/boot", response_model=schemas.WidgetBootResponse)
async def widget_boot(
    req: schemas.WidgetBootRequest, request: Request, response: Response
) -> schemas.WidgetBootResponse:
    """Boot a messenger session: verify identity (HMAC) or resume/create a cookie-scoped lead,
    then return a session token + public config + the contact's conversations."""
    workspace_id = _workspace_from_app_id(req.app_id)
    cookie_token = request.cookies.get(WIDGET_SESSION_COOKIE)
    async with session_scope(workspace_id) as session:
        result = await service.widget_boot(
            session, workspace_id=workspace_id, req=req, cookie_token=cookie_token
        )
    response.set_cookie(
        key=WIDGET_SESSION_COOKIE,
        value=result.session_token,
        max_age=get_settings().widget_session_ttl_seconds,
        httponly=True,
        secure=True,
        samesite="none",
        path="/v0",
    )
    return result


@router.get("/widget/conversations", response_model=Page[schemas.ConversationOut])
async def widget_list_conversations(
    contact: ContactSession,
    session: SessionDep,
    cursor: str | None = None,
    limit: int | None = Query(default=None, ge=1, le=200),
) -> Page[schemas.ConversationOut]:
    return await service.contact_list_conversations(session, contact, cursor=cursor, limit=limit)


@router.post("/widget/conversations", response_model=schemas.ConversationOut, status_code=201)
@idempotent(status_code=201)
async def widget_start_conversation(
    req: schemas.WidgetStartConversation,
    request: Request,
    principal: ContactSession,
    session: SessionDep,
) -> schemas.ConversationOut:
    return await service.contact_start_conversation(session, principal, req)


@router.get("/widget/conversations/{conversation_id}/parts", response_model=Page[schemas.PartOut])
async def widget_list_parts(
    conversation_id: str,
    contact: ContactSession,
    session: SessionDep,
    cursor: str | None = None,
    after: str | None = Query(
        default=None,
        description="Realtime long-poll fallback: parts newer than this part id, ascending.",
    ),
    limit: int | None = Query(default=None, ge=1, le=200),
) -> Page[schemas.PartOut]:
    return await service.contact_list_parts(
        session, contact, conversation_id, after=after, cursor=cursor, limit=limit
    )


@router.post(
    "/widget/conversations/{conversation_id}/reply",
    response_model=schemas.PartOut,
    status_code=201,
)
@idempotent(status_code=201)
async def widget_reply(
    conversation_id: str,
    req: schemas.WidgetReplyIn,
    request: Request,
    principal: ContactSession,
    session: SessionDep,
) -> schemas.PartOut:
    return await service.contact_reply(session, principal, conversation_id, req)


@router.post(
    "/widget/conversations/{conversation_id}/rating",
    response_model=schemas.PartOut,
    status_code=201,
)
@idempotent(status_code=201)
async def widget_rating(
    conversation_id: str,
    req: schemas.WidgetRatingIn,
    request: Request,
    principal: ContactSession,
    session: SessionDep,
) -> schemas.PartOut:
    return await service.contact_rate(session, principal, conversation_id, req)


@router.post("/widget/conversations/{conversation_id}/typing", status_code=204)
async def widget_typing(
    conversation_id: str, contact: ContactSession, session: SessionDep
) -> Response:
    await service.contact_typing(session, contact, conversation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/widget/conversations/{conversation_id}/realtime-token",
    response_model=schemas.RealtimeTokenOut,
)
async def widget_realtime_token(
    conversation_id: str, contact: ContactSession, session: SessionDep
) -> schemas.RealtimeTokenOut:
    return await service.contact_realtime_token(session, contact, conversation_id)


# --- Office hours (P1.7) ------------------------------------------------------
#
# Workspace + per-team business-hours schedules. Reads are open to any agent; writes are admin-only
# (enforced in the service ``authorize`` choke point). Upsert is a PUT keyed on (workspace, team).


@router.get("/office-hours", response_model=list[schemas.OfficeHoursScheduleOut])
async def list_office_hours(
    _principal: CurrentPrincipal, session: SessionDep
) -> list[schemas.OfficeHoursScheduleOut]:
    return await office_hours.list_schedules(session)


@router.get("/office-hours/status", response_model=schemas.OfficeHoursStatusOut)
async def office_hours_status(
    principal: CurrentPrincipal,
    session: SessionDep,
    team_id: str | None = Query(default=None),
) -> schemas.OfficeHoursStatusOut:
    """Whether the effective schedule (team override → workspace default) is open right now."""
    return await office_hours.status(session, principal, team_id)


@router.put("/office-hours", response_model=schemas.OfficeHoursScheduleOut)
async def upsert_office_hours(
    req: schemas.OfficeHoursScheduleIn,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> schemas.OfficeHoursScheduleOut:
    """Create or replace a schedule (admin-only). Omit ``team_id`` for the workspace default."""
    return await office_hours.upsert_schedule(session, principal, req)


@router.delete("/office-hours/{schedule_id}", status_code=204)
async def delete_office_hours(
    schedule_id: str, principal: CurrentPrincipal, session: SessionDep
) -> Response:
    await office_hours.delete_schedule(session, principal, schedule_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- SLA policies + per-conversation SLA (P1.7) -------------------------------
#
# Policy CRUD is admin-only (enforced in the service ``authorize`` choke point); reads and
# applying a policy to a conversation are agent+. Breach firing is durable (beat sweep), not here.


@router.get("/sla-policies", response_model=list[schemas.SlaPolicyOut])
async def list_sla_policies(
    _principal: CurrentPrincipal, session: SessionDep
) -> list[schemas.SlaPolicyOut]:
    return await sla.list_policies(session)


@router.post("/sla-policies", response_model=schemas.SlaPolicyOut, status_code=201)
async def create_sla_policy(
    req: schemas.SlaPolicyIn, principal: CurrentPrincipal, session: SessionDep
) -> schemas.SlaPolicyOut:
    return await sla.create_policy(session, principal, req)


@router.get("/sla-policies/{policy_id}", response_model=schemas.SlaPolicyOut)
async def get_sla_policy(
    policy_id: str, _principal: CurrentPrincipal, session: SessionDep
) -> schemas.SlaPolicyOut:
    return await sla.get_policy(session, policy_id)


@router.put("/sla-policies/{policy_id}", response_model=schemas.SlaPolicyOut)
async def update_sla_policy(
    policy_id: str, req: schemas.SlaPolicyIn, principal: CurrentPrincipal, session: SessionDep
) -> schemas.SlaPolicyOut:
    return await sla.update_policy(session, principal, policy_id, req)


@router.delete("/sla-policies/{policy_id}", status_code=204)
async def delete_sla_policy(
    policy_id: str, principal: CurrentPrincipal, session: SessionDep
) -> Response:
    await sla.delete_policy(session, principal, policy_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/conversations/{conversation_id}/sla", response_model=schemas.ConversationSlaOut)
async def get_conversation_sla(
    conversation_id: str, _principal: CurrentPrincipal, session: SessionDep
) -> schemas.ConversationSlaOut:
    return await sla.get_conversation_sla(session, conversation_id)


@router.post("/conversations/{conversation_id}/sla", response_model=schemas.ConversationSlaOut)
async def apply_conversation_sla(
    conversation_id: str,
    req: schemas.ApplySlaIn,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> schemas.ConversationSlaOut:
    return await sla.apply_sla(session, principal, conversation_id, req)


@router.delete("/conversations/{conversation_id}/sla", status_code=204)
async def remove_conversation_sla(
    conversation_id: str, principal: CurrentPrincipal, session: SessionDep
) -> Response:
    await sla.remove_sla(session, principal, conversation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Custom inbox views (P1.7) ------------------------------------------------
#
# Saved conversation filters. Any agent may create/read/update/delete a view (team-shared when
# ``team_id`` is set); the filter is a predicates AST validated + compiled on save. Listing reuses
# the R1 keyset ordering; the count is a short-TTL cached badge.


@router.get("/views", response_model=list[schemas.InboxViewOut])
async def list_views(
    _principal: CurrentPrincipal, session: SessionDep
) -> list[schemas.InboxViewOut]:
    return await views.list_views(session)


@router.post("/views", response_model=schemas.InboxViewOut, status_code=201)
async def create_view(
    req: schemas.InboxViewIn, principal: CurrentPrincipal, session: SessionDep
) -> schemas.InboxViewOut:
    return await views.create_view(session, principal, req)


@router.get("/views/{view_id}", response_model=schemas.InboxViewOut)
async def get_view(
    view_id: str, _principal: CurrentPrincipal, session: SessionDep
) -> schemas.InboxViewOut:
    return await views.get_view(session, view_id)


@router.put("/views/{view_id}", response_model=schemas.InboxViewOut)
async def update_view(
    view_id: str, req: schemas.InboxViewIn, principal: CurrentPrincipal, session: SessionDep
) -> schemas.InboxViewOut:
    return await views.update_view(session, principal, view_id, req)


@router.delete("/views/{view_id}", status_code=204)
async def delete_view(view_id: str, principal: CurrentPrincipal, session: SessionDep) -> Response:
    await views.delete_view(session, principal, view_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/views/{view_id}/conversations", response_model=Page[schemas.ConversationOut])
async def list_view_conversations(
    view_id: str,
    _principal: CurrentPrincipal,
    session: SessionDep,
    cursor: str | None = None,
    limit: int | None = Query(default=None, ge=1, le=200),
) -> Page[schemas.ConversationOut]:
    return await views.list_conversations_by_view(session, view_id, cursor=cursor, limit=limit)


@router.get("/views/{view_id}/count", response_model=schemas.ViewCountOut)
async def view_count(
    view_id: str, _principal: CurrentPrincipal, session: SessionDep
) -> schemas.ViewCountOut:
    return await views.view_count(session, view_id)


# --- Balanced assignment + agent availability (P1.7) --------------------------
#
# Load-aware assignment routes to the least-loaded eligible agent of a team. Availability (away /
# capacity) is self-managed by an agent, or set by an admin for any teammate.


@router.post(
    "/conversations/{conversation_id}/assign/balanced", response_model=schemas.ConversationOut
)
@idempotent(status_code=200)
async def assign_balanced(
    conversation_id: str,
    req: schemas.BalancedAssignIn,
    request: Request,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> schemas.ConversationOut:
    return await assignment.assign_balanced(session, principal, conversation_id, req)


@router.get("/me/availability", response_model=schemas.AgentAvailabilityOut)
async def get_my_availability(
    principal: CurrentPrincipal, session: SessionDep
) -> schemas.AgentAvailabilityOut:
    return await assignment.get_my_availability(session, principal)


@router.put("/me/availability", response_model=schemas.AgentAvailabilityOut)
async def set_my_availability(
    req: schemas.AgentAvailabilityIn, principal: CurrentPrincipal, session: SessionDep
) -> schemas.AgentAvailabilityOut:
    return await assignment.set_my_availability(session, principal, req)


@router.get("/availability", response_model=list[schemas.AgentAvailabilityOut])
async def list_availability(
    _principal: CurrentPrincipal, session: SessionDep
) -> list[schemas.AgentAvailabilityOut]:
    return await assignment.list_availability(session)


@router.put("/availability/{admin_id}", response_model=schemas.AgentAvailabilityOut)
async def set_availability(
    admin_id: str,
    req: schemas.AgentAvailabilityIn,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> schemas.AgentAvailabilityOut:
    return await assignment.set_availability(session, principal, admin_id, req)
