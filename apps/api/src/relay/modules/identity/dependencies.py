"""FastAPI dependencies for auth + tenancy.

``get_session`` is the single choke point for DB access: it opens the request's transaction
and sets the RLS GUC ``app.ws`` from the authenticated principal (established by
``TenancyMiddleware``). No handler may query a tenant table any other way, so no query path
can run without ``app.ws`` — and if somehow one does, RLS returns zero rows.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from relay.core.db import get_sessionmaker, set_workspace_guc
from relay.core.errors import AuthenticationError

from .principal import Principal
from .rbac import authorize


def _principal_from_request(request: Request) -> Principal | None:
    return getattr(request.state, "principal", None)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    principal = _principal_from_request(request)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        if principal is not None:
            await set_workspace_guc(session, principal.workspace_id)
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def require_principal(request: Request) -> Principal:
    principal = _principal_from_request(request)
    if principal is None:
        raise AuthenticationError("authentication required")
    return principal


CurrentPrincipal = Annotated[Principal, Depends(require_principal)]


def require_role(min_role: str) -> Callable[[Principal], Principal]:
    """Route guard factory. Enforces RBAC via the single ``authorize`` choke point."""

    def _dep(principal: CurrentPrincipal) -> Principal:
        authorize(principal, min_role=min_role)
        return principal

    return _dep
