"""Request-scoped context (contextvars).

Holds the correlation id and the authenticated principal's workspace so that logging
and the RLS session middleware can read them without threading arguments everywhere.
Populated by middleware (request id) and by the auth/session layer (workspace/principal).
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
workspace_id_var: ContextVar[uuid.UUID | None] = ContextVar("workspace_id", default=None)
admin_id_var: ContextVar[uuid.UUID | None] = ContextVar("admin_id", default=None)


def current_workspace_id() -> uuid.UUID | None:
    return workspace_id_var.get()


def current_admin_id() -> uuid.UUID | None:
    return admin_id_var.get()
