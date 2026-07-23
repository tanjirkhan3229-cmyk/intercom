"""Domain events emitted by the `billing` module onto the transactional outbox.

Event names are stable contracts consumed by other modules and by webhooks
(RFC-001 §6.5). Safe to import cross-module.
"""

from __future__ import annotations

SUBSCRIPTION_CREATED = "billing.subscription.created"
SUBSCRIPTION_UPDATED = "billing.subscription.updated"
SUBSCRIPTION_TRIAL_ENDING = "billing.subscription.trial_ending"
SUBSCRIPTION_CANCELED = "billing.subscription.canceled"
PAYMENT_FAILED = "billing.payment_failed"
PAYMENT_RECOVERED = "billing.payment_recovered"
SEATS_CHANGED = "billing.seats_changed"
# One unit of a metered resource was recorded (RFC-002 §5.6 W8). Async Stripe metering
# (P1.3) consumes this off the outbox so each unit meters to Stripe exactly once.
USAGE_RECORDED = "billing.usage.recorded"
