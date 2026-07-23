"""Webhook topic vocabulary + the outbox→webhook topic mapping (P0.11).

Outbox topics are internal (some module-prefixed, e.g. ``crm.contact.created``); the customer-
facing webhook topics are the stable public names the P0.11 prompt specifies. The dispatch
consumer filters the stream to ``SUBSCRIBABLE_OUTBOX_TOPICS`` and translates each to its public
topic; subscription create validates requested topics against ``WEBHOOK_TOPICS``.
"""

from __future__ import annotations

# Internal outbox topic (on the relay:outbox stream) -> public webhook topic (the API contract).
OUTBOX_TO_WEBHOOK_TOPIC: dict[str, str] = {
    "conversation.created": "conversation.created",
    "conversation.part.created": "conversation.part.created",
    "crm.contact.created": "contact.created",
    "crm.contact.updated": "contact.updated",
}

# What a subscription may subscribe to (customer-facing).
WEBHOOK_TOPICS: frozenset[str] = frozenset(OUTBOX_TO_WEBHOOK_TOPIC.values())
# What the dispatch consumer reacts to; every other stream topic is acked + ignored.
SUBSCRIBABLE_OUTBOX_TOPICS: frozenset[str] = frozenset(OUTBOX_TO_WEBHOOK_TOPIC)

# This module's own outbox events. When a subscription auto-disables after sustained failure we
# emit this in the same transaction as the disable (master rule 2) so a notifier can react later.
AGGREGATE_WEBHOOK_SUBSCRIPTION = "webhook_subscription"
SUBSCRIPTION_DISABLED = "webhook.subscription.disabled"
