"""Pure metrics reducer — folds a conversation's outbox events into a ``Metrics`` snapshot.

This is deliberately **I/O-free and deterministic**: it takes the current metrics + one event
(topic, payload, seq) and returns the next metrics. The ``reporting-metrics`` consumer
(``consumer.py``) is a thin shell that loads the row, calls :func:`apply_event`, and persists —
so the metric maths lives here, unit-tested against hand-computed fixtures (P0.9 acceptance 1)
without a database.

Idempotent replay: every event carries the per-aggregate outbox ``seq``. An event whose ``seq`` is
``<= last_seq`` has already been folded, so :func:`apply_event` returns the metrics unchanged. That
makes at-least-once redelivery (and full stream replay) safe.

Inputs are exactly the messaging event payloads (RFC-001 §6.5): public ids as prefixed base62,
timestamps as ISO-8601. The reducer decodes ids to raw UUIDs and parses timestamps so the consumer
can persist typed columns directly.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, replace

from relay.core.ids import IdPrefix, decode_public_id
from relay.modules.messaging import events

# Author kinds whose public comment counts as an agent reply (RFC-002 §5.3).
_AGENT_KINDS = frozenset({"admin", "ai_agent"})


@dataclass
class Metrics:
    """A conversation's rolled-up metrics. Mirrors the persisted ``conversation_metrics`` columns
    (minus the surrogate id / audit timestamps). ``None`` means "not yet observed"."""

    workspace_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    team_id: uuid.UUID | None = None
    assignee_id: uuid.UUID | None = None
    opened_at: dt.datetime | None = None
    first_admin_reply_at: dt.datetime | None = None
    first_response_s: int | None = None
    closed_at: dt.datetime | None = None
    resolution_s: int | None = None
    reopen_count: int = 0
    replies_count: int = 0
    rating: int | None = None
    rated_at: dt.datetime | None = None
    last_seq: int = 0


def _parse_dt(value: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(value) if value else None


def _decode(prefix: str, public_id: str | None) -> uuid.UUID | None:
    return decode_public_id(prefix, public_id) if public_id else None


def _elapsed_s(start: dt.datetime | None, end: dt.datetime | None) -> int | None:
    """Whole seconds between two instants, floored at 0 (never negative on clock skew)."""
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds()))


def apply_event(metrics: Metrics, topic: str, payload: dict[str, object], seq: int) -> Metrics:
    """Fold one outbox event into ``metrics``, returning the next snapshot.

    Pure: returns a new ``Metrics`` (never mutates the input). Out-of-window or already-applied
    events (``seq <= metrics.last_seq``) are a no-op — the idempotency guarantee.
    """
    if seq <= metrics.last_seq:
        return metrics

    m = replace(metrics, last_seq=seq)

    # Identity is immutable; refresh it defensively from every event.
    ws = _decode(IdPrefix.WORKSPACE, _str(payload.get("workspace_id")))
    cnv = _decode(IdPrefix.CONVERSATION, _str(payload.get("conversation_id")))
    if ws is not None:
        m.workspace_id = ws
    if cnv is not None:
        m.conversation_id = cnv
    # ATTRIBUTION: team_id is the FIRST team the conversation is seen under, latched once then
    # immutable (NULL->team at most once; never team->team'). Conversations often open team-less
    # and get routed a moment later, so latching strictly at create would strand them in the
    # unassigned bucket; first-observed captures the handling team. Immutability keeps team-filtered
    # reports consistent, and the rollup orphan-delete re-buckets that single NULL->team transition
    # without double-counting. assignee_id is the current assignee (informational only), refreshed
    # every event.
    team = _decode(IdPrefix.TEAM, _str(payload.get("team_id")))
    if m.team_id is None and team is not None:
        m.team_id = team
    m.assignee_id = _decode(IdPrefix.ADMIN, _str(payload.get("assignee_id")))

    if topic == events.CONVERSATION_CREATED:
        m.opened_at = _parse_dt(_str(payload.get("occurred_at")))

    elif topic == events.CONVERSATION_PART_CREATED:
        part_type = _str(payload.get("part_type"))
        author_kind = _str(payload.get("author_kind"))
        created_at = _parse_dt(_str(payload.get("created_at")))
        if part_type == "comment" and author_kind in _AGENT_KINDS:
            m.replies_count += 1
            if m.first_admin_reply_at is None:
                m.first_admin_reply_at = created_at
                m.first_response_s = _elapsed_s(m.opened_at, created_at)
        elif part_type == "rating":
            rating = payload.get("rating")
            if isinstance(rating, int) and not isinstance(rating, bool):
                m.rating = rating
                m.rated_at = created_at

    elif topic == events.CONVERSATION_STATE_CHANGED:
        occurred_at = _parse_dt(_str(payload.get("occurred_at")))
        to_state = _str(payload.get("to"))
        from_state = _str(payload.get("from"))
        if to_state == "closed":
            m.closed_at = occurred_at
            m.resolution_s = _elapsed_s(m.opened_at, occurred_at)
        elif to_state == "open" and from_state == "closed":
            m.reopen_count += 1
            m.closed_at = None
            m.resolution_s = None

    # CONVERSATION_ASSIGNED carries only routing, already refreshed above.
    return m


def _str(value: object) -> str | None:
    """Narrow an untyped JSON value to ``str | None`` (payloads are ``dict[str, object]``)."""
    return value if isinstance(value, str) else None


def fold(events_seq: list[tuple[str, dict[str, object], int]]) -> Metrics:
    """Fold an ordered ``(topic, payload, seq)`` sequence from empty — the unit-test entry point."""
    metrics = Metrics()
    for topic, payload, seq in events_seq:
        metrics = apply_event(metrics, topic, payload, seq)
    return metrics
