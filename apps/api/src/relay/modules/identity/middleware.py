"""Session/tenancy middleware (RFC-002 §7; P0.11 API-key auth).

Authenticates the request and records the principal on ``request.state`` plus the logging
contextvars. The DB session provider (``core.deps.get_session``) then opens the request
transaction with ``SET LOCAL app.ws`` derived from that principal. Three credential types:

- **JWT** (``Authorization: Bearer <access-jwt>``) — an agent. Pure, no DB read.
- **Widget session** (a contact/lead) — a ``ContactPrincipal``. Pure, no DB read.
- **API key** (``Authorization: Bearer relaysk_…``) — a third party (P0.11). The workspace is
  read from the key's embedded prefix, then the key is verified **under RLS pinned to that
  workspace** (never a bypass): a spoofed prefix or a foreign key yields zero rows.

Unauthenticated requests carry no principal; protected routes reject them via ``require_principal``
and RLS is the backstop.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from contextvars import Token
from typing import Any

import jwt
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from relay.core.api_key import looks_like_api_key, parse_api_key
from relay.core.context import admin_id_var, role_var, workspace_id_var
from relay.core.db import session_scope
from relay.core.principal import ContactPrincipal
from relay.core.rbac import Role
from relay.core.security import decode_access_token, decode_widget_session_token, hash_secret

from .models import ApiKey
from .principal import Principal


def _bearer(request: Request) -> str | None:
    header = request.headers.get("Authorization")
    if not header or not header.lower().startswith("bearer "):
        return None
    return header[7:].strip()


def _authenticate(token: str) -> Principal | None:
    try:
        claims = decode_access_token(token)
        return Principal(
            admin_id=uuid.UUID(claims["sub"]),
            workspace_id=uuid.UUID(claims["ws"]),
            role=claims["role"],
        )
    except (jwt.PyJWTError, KeyError, ValueError):
        return None


def _authenticate_contact(token: str) -> ContactPrincipal | None:
    """A widget end-user (contact/lead) session token. Disjoint from agent tokens by ``type``."""
    try:
        claims = decode_widget_session_token(token)
        return ContactPrincipal(
            workspace_id=uuid.UUID(claims["ws"]),
            contact_id=uuid.UUID(claims["sub"]),
        )
    except (jwt.PyJWTError, KeyError, ValueError):
        return None


def _role_for_scopes(scopes: list[str]) -> str:
    """Map API-key scopes to the RBAC role used by service-layer ``authorize`` checks.

    A ``write`` key acts at **agent** level — enough for the public mutations (identify, create
    contact, reply, track) but never admin, so a key can never reach an admin-gated service path
    even if a route leaked. A read-only/empty key maps to ``restricted`` (rank 0): it passes GETs
    (which don't authorize) and is blocked from anything requiring ≥ agent.
    """
    return Role.AGENT if "write" in scopes else Role.RESTRICTED


async def _authenticate_api_key(raw_key: str, request: Request) -> Principal | None:
    try:
        workspace_id = parse_api_key(raw_key)
    except ValueError:
        return None
    # Verify UNDER RLS pinned to the parsed workspace — never a bypass. ``key_hash`` is globally
    # unique and hashes the *whole* key (prefix included), so a tampered prefix changes the hash
    # (no match) and a foreign-workspace key is filtered by RLS (its real workspace_id ≠ parsed).
    async with session_scope(workspace_id) as session:
        key = (
            await session.scalars(select(ApiKey).where(ApiKey.key_hash == hash_secret(raw_key)))
        ).one_or_none()
    # ``created_by`` is the principal's admin_id (kept non-optional, per the ContactPrincipal
    # design). A key whose creating admin was deleted (created_by NULL) is rejected — recreate it.
    if key is None or key.revoked_at is not None or key.created_by is None:
        return None
    request.state.api_key_id = key.id
    return Principal(
        admin_id=key.created_by,
        workspace_id=workspace_id,
        role=_role_for_scopes(list(key.scopes)),
        kind="api_key",
        scopes=tuple(key.scopes),
    )


class TenancyMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        token = _bearer(request)
        principal: Principal | None = None
        contact: ContactPrincipal | None = None
        if token is not None:
            if looks_like_api_key(token):
                principal = await _authenticate_api_key(token, request)
            else:
                principal = _authenticate(token)
                # A single bearer token is one audience or the other, never both.
                if principal is None:
                    contact = _authenticate_contact(token)
        request.state.principal = principal
        request.state.widget = contact

        tokens: list[Token[Any]] = []
        if principal is not None:
            tokens.append(workspace_id_var.set(principal.workspace_id))
            tokens.append(admin_id_var.set(principal.admin_id))
            tokens.append(role_var.set(principal.role))
        elif contact is not None:
            tokens.append(workspace_id_var.set(contact.workspace_id))
        try:
            return await call_next(request)
        finally:
            if principal is not None:
                role_var.reset(tokens[2])
                admin_id_var.reset(tokens[1])
                workspace_id_var.reset(tokens[0])
            elif contact is not None:
                workspace_id_var.reset(tokens[0])
