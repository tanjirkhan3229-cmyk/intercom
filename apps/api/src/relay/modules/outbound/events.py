"""Domain events emitted by the ``outbound`` module onto the transactional outbox (RFC-001 §6.5).

Event names are stable contracts consumed by other modules, the stats consumer, the
realtime-fanout consumer, and webhooks. Safe to import cross-module.

Ordering: campaign lifecycle + per-send + per-engagement events all use ``aggregate='campaign'``
with ``aggregate_id = campaign_id`` so the outbox assigns a monotonic per-campaign ``seq`` — the
stats reducer relies on that ``seq`` for its idempotency watermark. Post delivery uses
``aggregate='post'``; consent changes use ``aggregate='consent'`` keyed on the consents row id.
"""

from __future__ import annotations

# --- Aggregates (outbox ``aggregate`` values) --------------------------------------------------
AGGREGATE_CAMPAIGN = "campaign"
AGGREGATE_POST = "post"
AGGREGATE_CONSENT = "consent"

# --- Campaign lifecycle ------------------------------------------------------------------------
# Consumed by the outbound-fire consumer (snapshot + chunked enqueue).
CAMPAIGN_FIRED = "campaign.fired"

# --- Per-send results (folded into campaign_stats by the outbound-stats consumer) --------------
CAMPAIGN_SEND_SENT = "campaign.send.sent"
CAMPAIGN_SEND_SKIPPED = "campaign.send.skipped"
CAMPAIGN_SEND_FAILED = "campaign.send.failed"

# --- Per-engagement events from provider webhooks (folded into campaign_stats) -----------------
CAMPAIGN_EVENT_DELIVERED = "campaign.event.delivered"
CAMPAIGN_EVENT_OPEN = "campaign.event.open"
CAMPAIGN_EVENT_CLICK = "campaign.event.click"
CAMPAIGN_EVENT_BOUNCE = "campaign.event.bounce"
CAMPAIGN_EVENT_COMPLAINT = "campaign.event.complaint"
CAMPAIGN_EVENT_UNSUB = "campaign.event.unsub"

# Topic prefix the stats consumer filters on (everything above except CAMPAIGN_FIRED).
CAMPAIGN_STATS_PREFIXES = ("campaign.send.", "campaign.event.")

# Map a stats topic to the campaign_stats counter it increments.
STATS_COUNTER_BY_TOPIC: dict[str, str] = {
    CAMPAIGN_SEND_SENT: "sent",
    CAMPAIGN_SEND_SKIPPED: "skipped",
    CAMPAIGN_SEND_FAILED: "failed",
    CAMPAIGN_EVENT_DELIVERED: "delivered",
    CAMPAIGN_EVENT_OPEN: "opened",
    CAMPAIGN_EVENT_CLICK: "clicked",
    CAMPAIGN_EVENT_BOUNCE: "bounced",
    CAMPAIGN_EVENT_COMPLAINT: "complained",
    CAMPAIGN_EVENT_UNSUB: "unsubscribed",
}

# --- In-app posts/chats --------------------------------------------------------------------
# Consumed by the outbound-fire consumer (snapshot + chunked delivery), like CAMPAIGN_FIRED.
POST_FIRED = "outbound.post.fired"
# Delivery event mapped to a per-contact Centrifugo channel by realtime-fanout.
POST_DELIVERED = "outbound.post.delivered"

# --- Consent (audit + webhook fan-out; not folded into stats) ----------------------------------
CONSENT_CHANGED = "outbound.consent.changed"
