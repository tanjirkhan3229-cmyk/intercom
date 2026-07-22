"""Re-export of the kernel :class:`Principal` (moved to ``relay.core.principal``).

Kept as a stable import path for the identity module and its tests. New code should import
from ``relay.core.principal`` directly.
"""

from __future__ import annotations

from relay.core.principal import Principal

__all__ = ["Principal"]
