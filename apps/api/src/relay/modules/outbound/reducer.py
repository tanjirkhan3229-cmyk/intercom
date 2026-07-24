"""Pure campaign-stats reducer — folds a campaign's outbox events into a ``CampaignStatsAgg``.

I/O-free and deterministic (mirrors ``reporting.reducer``): the ``outbound-stats`` consumer loads
the row, calls :func:`apply_event`, and upserts. Idempotent replay: every event carries a
per-campaign outbox ``seq``; an event whose ``seq <= last_seq`` has already been folded and is a
no-op, so at-least-once redelivery and full stream replay converge to the same counters.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace

from relay.core.ids import IdPrefix, decode_public_id

from . import events


@dataclass
class CampaignStatsAgg:
    """A campaign's rolled-up counters. Mirrors the persisted ``campaign_stats`` columns the reducer
    owns (``audience_size`` is set by the snapshot, not folded here)."""

    workspace_id: uuid.UUID | None = None
    campaign_id: uuid.UUID | None = None
    sent: int = 0
    delivered: int = 0
    opened: int = 0
    clicked: int = 0
    bounced: int = 0
    complained: int = 0
    unsubscribed: int = 0
    skipped: int = 0
    failed: int = 0
    last_seq: int = 0


def _decode(prefix: str, value: object) -> uuid.UUID | None:
    return decode_public_id(prefix, value) if isinstance(value, str) else None


def apply_event(
    agg: CampaignStatsAgg, topic: str, payload: dict[str, object], seq: int
) -> CampaignStatsAgg:
    """Fold one campaign event into ``agg``, returning the next snapshot (pure; never mutates)."""
    if seq <= agg.last_seq:
        return agg
    m = replace(agg, last_seq=seq)

    ws = _decode(IdPrefix.WORKSPACE, payload.get("workspace_id"))
    campaign = _decode(IdPrefix.CAMPAIGN, payload.get("campaign_id"))
    if ws is not None:
        m.workspace_id = ws
    if campaign is not None:
        m.campaign_id = campaign

    counter = events.STATS_COUNTER_BY_TOPIC.get(topic)
    if counter is not None:
        setattr(m, counter, getattr(m, counter) + 1)
    return m


# The counter columns the reducer owns (for row <-> agg mapping in the consumer).
STATS_FIELDS = (
    "sent",
    "delivered",
    "opened",
    "clicked",
    "bounced",
    "complained",
    "unsubscribed",
    "skipped",
    "failed",
    "last_seq",
)
