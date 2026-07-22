"""Domain events emitted by the `messaging` module onto the transactional outbox.

Event names are stable contracts consumed by other modules and by webhooks
(RFC-001 §6.5). Safe to import cross-module.
"""

from __future__ import annotations
