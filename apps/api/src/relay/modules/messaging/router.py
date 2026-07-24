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

from . import schemas, service

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


@router.post(
    "/widget/conversations/{conversation_id}/resolve",
    response_model=schemas.ConversationOut,
)
async def widget_confirm_resolution(
    conversation_id: str, principal: ContactSession, session: SessionDep
) -> schemas.ConversationOut:
    """The visitor confirms Neko resolved their question (RFC-003 §8) — meters the resolution if it
    qualifies, in the same txn as the close."""
    return await service.contact_confirm_resolution(session, principal, conversation_id)


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
