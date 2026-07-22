"""Re-export of the kernel RBAC choke point (moved to ``relay.core.rbac``, RFC-001 §10).

Kept as a stable import path for the identity module and its tests. New code should import
from ``relay.core.rbac`` directly.
"""

from __future__ import annotations

from relay.core.rbac import ROLE_RANK, Role, authorize, role_at_least

__all__ = ["ROLE_RANK", "Role", "authorize", "role_at_least"]
