"""Shared FastAPI dependencies for auth + tenancy (RFC-002 §7).

``get_session`` is the single choke point for DB access: it opens the request's transaction
and sets the RLS GUC ``app.ws`` from the authenticated principal (established by the identity
module's ``TenancyMiddleware``, which records it on ``request.state``). No handler may query a
tenant table any other way, so no query path can run without ``app.ws`` — and if somehow one
does, RLS returns zero rows.

Lives in ``relay.core`` so every feature module's router can build authenticated, tenant-
scoped routes without importing another module's internals (the boundary rule allows
``relay.core`` imports everywhere; cross-module imports are limited to ``service``/``events``).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from relay.core.db import get_sessionmaker, set_workspace_guc
from relay.core.errors import AuthenticationError
from relay.core.principal import ContactPrincipal, Principal
from relay.core.rbac import authorize


def _principal_from_request(request: Request) -> Principal | None:
    return getattr(request.state, "principal", None)


def _contact_from_request(request: Request) -> ContactPrincipal | None:
    return getattr(request.state, "widget", None)


def _workspace_from_request(request: Request) -> uuid.UUID | None:
    """The tenant to scope RLS to — from the agent principal or the widget contact session,
    whichever authenticated the request (they never coexist)."""
    principal = _principal_from_request(request)
    if principal is not None:
        return principal.workspace_id
    contact = _contact_from_request(request)
    if contact is not None:
        return contact.workspace_id
    return None


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    workspace_id = _workspace_from_request(request)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        if workspace_id is not None:
            await set_workspace_guc(session, workspace_id)
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def require_principal(request: Request) -> Principal:
    principal = _principal_from_request(request)
    if principal is None:
        raise AuthenticationError("authentication required")
    return principal


CurrentPrincipal = Annotated[Principal, Depends(require_principal)]


def require_contact(request: Request) -> ContactPrincipal:
    """Widget routes: require an authenticated end-user (contact/lead) session, never an agent."""
    contact = _contact_from_request(request)
    if contact is None:
        raise AuthenticationError("widget session required")
    return contact


ContactSession = Annotated[ContactPrincipal, Depends(require_contact)]


def require_role(min_role: str) -> Callable[[Principal], Principal]:
    """Route guard factory. Enforces RBAC via the single ``authorize`` choke point."""

    def _dep(principal: CurrentPrincipal) -> Principal:
        authorize(principal, min_role=min_role)
        return principal

    return _dep
