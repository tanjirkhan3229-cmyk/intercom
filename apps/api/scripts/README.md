# Billing manual verification — Stripe test clock

`stripe_test_clock.py` drives the P0.10 acceptance lifecycle —
**trial → subscribe → seat add → payment fail → recovery** — against Stripe's real
**test-mode** API using a [test clock](https://stripe.com/docs/billing/testing/test-clocks),
which lets a 14-day trial and a billing cycle be fast-forwarded in seconds.

This is a **manual** harness. It makes real (test-mode) network calls, so it is not part of
`make test-api` / CI. The automated equivalent (fake Stripe client + hand-signed webhooks) lives
in `tests/integration/test_billing.py` and runs in CI.

## Prerequisites

1. A Stripe **test-mode** secret key (`sk_test_...`). The script refuses any `sk_live_` key.
2. A **recurring monthly price** in that test account. Create one if needed:

   ```bash
   # a $20/mo product+price in test mode
   stripe products create --name "Relay Team (test)"
   stripe prices create --unit-amount 2000 --currency usd \
     --recurring[interval]=month --product <prod_id_from_above>
   ```

   Copy the resulting `price_...` id.

## Run it

```bash
export STRIPE_SECRET_KEY=sk_test_...
export STRIPE_PRICE_ID=price_...
# optional, defaults shown:
# export STRIPE_API_VERSION=2024-06-20
# export STRIPE_API_BASE=https://api.stripe.com

python apps/api/scripts/stripe_test_clock.py
```

Expected output (abridged):

```
1. Create test clock (frozen at now)
2. Create customer on the clock + attach a working card
3. Subscribe with a 14-day trial → trialing
  ✓ trial: status='trialing' (expected 'trialing')
4. Advance past trial end (now + 15 days) → active
  ✓ subscribe: status='active' (expected 'active')
5. Seat add: update quantity 1 → 2
  ✓ seat quantity: 2 (expected 2)
6. Payment fail: switch to a failing card, advance a full cycle → past_due
  ✓ payment_fail: status='past_due' (expected 'past_due')
7. Recovery: restore a working card, pay the open invoice → active
  ✓ recovery: status='active' (expected 'active')

All lifecycle transitions verified ✓
```

Exit code `0` means every state transition matched; any mismatch prints `FAIL at step ...`
and exits non-zero.

## Also verifying the states *in-app* (the other half of the acceptance criterion)

Run the app (`make dev`) and forward Stripe events to the local webhook so the same lifecycle
updates `subscriptions.status` / `banner_state` in Postgres:

```bash
# in one terminal — forwards test-mode events to the app's webhook
stripe listen --forward-to localhost:8000/v0/billing/webhook
# copy the printed `whsec_...` into STRIPE_WEBHOOK_SECRET in .env, restart the api container
```

Then run the harness in another terminal. As it advances the clock, watch:

```bash
# owner-authenticated call; shows status + banner_state flipping trialing→active→past_due→active
curl -s localhost:8000/v0/billing/subscription -H "Authorization: Bearer <owner-jwt>" | jq
```

The banner state should track: `none` (trialing/active) → `payment_failed` (past_due) →
`none` (recovered).
