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
