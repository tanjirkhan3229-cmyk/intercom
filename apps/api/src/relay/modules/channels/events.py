"""Domain events emitted by the ``channels`` module onto the transactional outbox.

Topic names are stable contracts consumed by other subsystems (webhooks P0.11, reporting) —
safe to import cross-module. Each is written with ``relay.core.outbox.emit`` in the same
transaction as the domain write (master rule 2).
"""

from __future__ import annotations

# Aggregates (outbox per-aggregate ordering key).
AGGREGATE_EMAIL_DOMAIN = "email_domain"
AGGREGATE_SUPPRESSION = "suppression"

# Topics (``<module>.<aggregate>.<verb>``).
EMAIL_DOMAIN_VERIFIED = "channels.email_domain.verified"
EMAIL_ADDRESS_SUPPRESSED = "channels.suppression.created"
