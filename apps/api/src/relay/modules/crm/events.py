"""Domain events emitted by the ``crm`` module (RFC-001 §6.5).

Event names are stable contracts consumed by other modules and by webhooks. In P0.2 the
transactional outbox does not exist yet (it lands in P0.3); these constants define the topic
vocabulary now so downstream wiring is a one-line change once the outbox relay is in place.
Safe to import cross-module (import-linter allows ``modules.* -> modules.*.events``).
"""

from __future__ import annotations

# Topic constants (``<module>.<aggregate>.<verb>``).
CONTACT_CREATED = "crm.contact.created"
CONTACT_UPDATED = "crm.contact.updated"
CONTACT_IDENTIFIED = "crm.contact.identified"
CONTACT_DELETED = "crm.contact.deleted"
COMPANY_CREATED = "crm.company.created"
COMPANY_UPDATED = "crm.company.updated"

# Aggregate name used on the outbox (``outbox.aggregate``) for contact-scoped events.
AGGREGATE_CONTACT = "contact"
AGGREGATE_COMPANY = "company"
