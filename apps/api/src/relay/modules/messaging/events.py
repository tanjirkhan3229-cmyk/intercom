"""Domain events emitted by the ``messaging`` module onto the transactional outbox.

Topic names are stable contracts consumed by other subsystems (realtime fan-out P0.4, webhooks
P0.11, reporting P0.9) — safe to import cross-module. Each W1/W4 transaction writes an outbox
row with one of these topics via ``relay.core.outbox.emit``; the ``aggregate`` is always
``conversation`` and the ``aggregate_id`` the conversation's uuid, so per-aggregate ordering
tracks the thread.
"""

from __future__ import annotations

# Aggregate name for every conversation-scoped event (outbox per-aggregate ordering key).
AGGREGATE_CONVERSATION = "conversation"

# Topics (RFC-001 §6.5). Consumers subscribe by topic.
CONVERSATION_CREATED = "conversation.created"
CONVERSATION_PART_CREATED = "conversation.part.created"
CONVERSATION_STATE_CHANGED = "conversation.state_changed"
CONVERSATION_ASSIGNED = "conversation.assigned"
# P1.7 SLA breach — fired by the breach sweep (per target) so downstream (webhooks/realtime/
# notifications) can react. Rides the conversation aggregate for per-thread ordering. (Applying a
# policy is recorded in ``sla_events`` for reporting, not on the aggregate stream.)
CONVERSATION_SLA_BREACHED = "conversation.sla_breached"
