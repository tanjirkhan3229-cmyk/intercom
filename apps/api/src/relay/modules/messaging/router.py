"""HTTP routes for the ``messaging`` module (RFC-002 §5.3). Mounted by relay.main under ``/v0``.

Auth/tenancy come from the shared kernel (``relay.core.deps``); RBAC is enforced in the service
layer through the ``authorize`` choke point. Mutating endpoints carry ``@idempotent`` so a
retried request with the same ``Idempotency-Key`` header replays the original response and
creates exactly one row (RFC-002 §7). List endpoints are keyset-paginated.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request, Response, status

from relay.core.deps import CurrentPrincipal, SessionDep
from relay.core.idempotency import idempotent
from relay.core.pagination import Page

from . import schemas, service

router = APIRouter(tags=["messaging"])


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
    cursor: str | None = None,
    limit: int | None = Query(default=None, ge=1, le=200),
) -> Page[schemas.ConversationOut]:
    return await service.list_conversations(
        session,
        state=state,
        team_id=team_id,
        assignee_id=assignee_id,
        cursor=cursor,
        limit=limit,
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
    limit: int | None = Query(default=None, ge=1, le=200),
) -> Page[schemas.PartOut]:
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
