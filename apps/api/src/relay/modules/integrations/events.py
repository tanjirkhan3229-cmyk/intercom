"""Outbox topics the Slack notifier reacts to (P1.9).

Only conversation lifecycle events are surfaced to Slack. For ``conversation.part.created`` we
notify **only on contact-authored parts**: an admin reply (including one that arrived *from* Slack)
must not be echoed back, which would otherwise create a notify → reply → notify loop.
"""

from __future__ import annotations

CONVERSATION_CREATED = "conversation.created"
CONVERSATION_PART_CREATED = "conversation.part.created"

SUBSCRIBABLE_OUTBOX_TOPICS: frozenset[str] = frozenset(
    {CONVERSATION_CREATED, CONVERSATION_PART_CREATED}
)
