"""Domain events + trigger mapping for the ``automation`` module (P1.5, RFC-001 §6.5/§6.7).

Two directions:

- **Outbound** — the workflow engine emits its own lifecycle events on the outbox (a run started /
  completed / failed), so reporting/webhooks/etc. can react later. Aggregate = the workflow run.
- **Inbound** — the trigger consumer subscribes to *other* modules' outbox topics and maps each to a
  workflow **trigger key** (the value stored on a version's trigger node).
  ``conversation.part.created`` is special: it only becomes the ``contact.message.created`` trigger
  when the part's author is the contact (an agent/bot reply must not fire a "customer" workflow).

Safe to import cross-module (import-linter allows ``modules.* -> modules.*.events``).
"""

from __future__ import annotations

from typing import Any

# --- Outbound: this module's own outbox events --------------------------------

AGGREGATE_WORKFLOW_RUN = "workflow_run"

WORKFLOW_RUN_STARTED = "automation.workflow_run.started"
WORKFLOW_RUN_COMPLETED = "automation.workflow_run.completed"
WORKFLOW_RUN_FAILED = "automation.workflow_run.failed"

# --- Inbound: outbox topics the trigger consumer reacts to --------------------

# Direct (payload-independent) outbox-topic → trigger-key mappings.
_DIRECT_OUTBOX_TO_TRIGGER: dict[str, str] = {
    "conversation.created": "conversation.created",
    "crm.contact.created": "contact.created",
    "crm.contact.updated": "contact.updated",
    "conversation.state_changed": "conversation.state_changed",
}

# Every outbox topic the consumer must inspect (others are acked + ignored). Includes
# ``conversation.part.created`` for the author-conditional ``contact.message.created`` mapping.
SUBSCRIBED_OUTBOX_TOPICS: frozenset[str] = frozenset(
    {*_DIRECT_OUTBOX_TO_TRIGGER, "conversation.part.created"}
)


def trigger_key_for(topic: str, payload: dict[str, Any]) -> str | None:
    """Map an outbox ``(topic, payload)`` to a workflow trigger key, or ``None`` if it should not
    fire any workflow (e.g. an agent/bot part, which is not a *customer* message)."""
    if topic == "conversation.part.created":
        if payload.get("part_type") == "comment" and payload.get("author_kind") == "contact":
            return "contact.message.created"
        return None
    return _DIRECT_OUTBOX_TO_TRIGGER.get(topic)
