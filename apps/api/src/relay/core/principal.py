"""The authenticated principal — passed to services so RBAC is checked in one place.

Lives in the shared kernel (not a feature module) because every module's router/service
receives it. The identity module authenticates the request and builds it; other modules
import it from here (the boundary rule allows ``relay.core`` imports everywhere).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    admin_id: uuid.UUID
    workspace_id: uuid.UUID
    role: str
    kind: str = "admin"  # "admin" (JWT) | "api_key"


@dataclass(frozen=True)
class ContactPrincipal:
    """An end-user (widget contact/lead) session — deliberately *not* a :class:`Principal`.

    Contacts never hold an RBAC role, so keeping them a separate type means every agent code
    path stays typed around ``Principal`` (no ``admin_id | None`` cascade) and can never be
    handed a contact by accident. Both types carry ``workspace_id`` so the RLS session provider
    (``core.deps.get_session``) sets ``app.ws`` from whichever authenticated the request.
    """

    workspace_id: uuid.UUID
    contact_id: uuid.UUID
    kind: str = "contact"
