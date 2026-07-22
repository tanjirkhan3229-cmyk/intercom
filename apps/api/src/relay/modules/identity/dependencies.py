"""Re-export of the shared auth/tenancy dependencies (moved to ``relay.core.deps``).

Kept as a stable import path for the identity module and its tests. New code should import
from ``relay.core.deps`` directly.
"""

from __future__ import annotations

from relay.core.deps import (
    CurrentPrincipal,
    SessionDep,
    get_session,
    require_principal,
    require_role,
)

__all__ = [
    "CurrentPrincipal",
    "SessionDep",
    "get_session",
    "require_principal",
    "require_role",
]
