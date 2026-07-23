"""Domain events for the `ai` module (RFC-001 §6.5).

P1.2 deliberately emits **no dedicated ``ai.*`` outbox topic**. A turn's externally-visible effects
already ride existing conversation events written in the finalize transaction (master rule 2):
``conversation.part.created`` for the answer / handoff note, and ``conversation.ai_status_changed``
for the status flip — both fanned to realtime and available to webhooks. Analytics (P1.4) reads the
``agent_runs`` ledger directly. ``TURN_COMPLETED`` is reserved for a future webhook surface;
it is not emitted yet (emitting it on the conversation aggregate would need the head row lock, which
the ``ineligible`` terminal does not take — so it waits for a deliberate design in P1.4).
"""

from __future__ import annotations

# Reserved (P1.4) — not emitted in P1.2.
AGGREGATE_AGENT_RUN = "agent_run"
TURN_COMPLETED = "ai.turn.completed"
