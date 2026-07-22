"""Session/tenancy middleware (RFC-002 §7).

Authenticates the request from the ``Authorization: Bearer <access-jwt>`` header and, when
valid, records the principal on ``request.state`` plus the logging contextvars. The DB
session provider (``dependencies.get_session``) then opens the request transaction with
``SET LOCAL app.ws`` derived from that principal. Unauthenticated requests carry no
principal; protected routes reject them via ``require_principal`` and RLS is the backstop.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from contextvars import Token
from typing import Any

import jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from relay.core.context import admin_id_var, role_var, workspace_id_var
from relay.core.security import decode_access_token

from .principal import Principal


def _authenticate(request: Request) -> Principal | None:
    header = request.headers.get("Authorization")
    if not header or not header.lower().startswith("bearer "):
        return None
    token = header[7:].strip()
    try:
        claims = decode_access_token(token)
        return Principal(
            admin_id=uuid.UUID(claims["sub"]),
            workspace_id=uuid.UUID(claims["ws"]),
            role=claims["role"],
        )
    except (jwt.PyJWTError, KeyError, ValueError):
        return None


class TenancyMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        principal = _authenticate(request)
        request.state.principal = principal

        tokens: list[Token[Any]] = []
        if principal is not None:
            tokens.append(workspace_id_var.set(principal.workspace_id))
            tokens.append(admin_id_var.set(principal.admin_id))
            tokens.append(role_var.set(principal.role))
        try:
            return await call_next(request)
        finally:
            if principal is not None:
                role_var.reset(tokens[2])
                admin_id_var.reset(tokens[1])
                workspace_id_var.reset(tokens[0])
