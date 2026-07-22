"""The authenticated principal — passed to services so RBAC is checked in one place."""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    admin_id: uuid.UUID
    workspace_id: uuid.UUID
    role: str
    kind: str = "admin"  # "admin" (JWT) | "api_key"
