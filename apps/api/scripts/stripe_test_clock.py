#!/usr/bin/env python3
"""Stripe **test-clock** acceptance harness for Billing v1 (P0.10, RFC-002 §5.6).

Drives the full lifecycle the P0.10 acceptance criterion names —
**trial → subscribe → seat add → payment fail → recovery** — against Stripe's real
*test-mode* API using a `test clock <https://stripe.com/docs/billing/testing/test-clocks>`_,
so time can be fast-forwarded through a 14-day trial and a billing cycle in seconds. Every
call is test-mode only; the script refuses a live (``sk_live_``) key.

It asserts the subscription's Stripe-side ``status`` at each step. If you also run the Stripe
CLI forwarder (see scripts/README.md), the same events reach the app's webhook and you can
confirm the in-app banner/status states in parallel — that closes the "all states reflected
in-app" half of the acceptance criterion.

This is a MANUAL harness: it makes real (test-mode) network calls and is never run in CI.

Usage:
    export STRIPE_SECRET_KEY=sk_test_...        # test-mode secret key
    export STRIPE_PRICE_ID=price_...            # a recurring (monthly) price in that account
    python apps/api/scripts/stripe_test_clock.py

Exit code 0 = every state transition observed as expected; non-zero = a mismatch (printed).
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

import httpx

STRIPE_API_BASE = os.environ.get("STRIPE_API_BASE", "https://api.stripe.com")
STRIPE_API_VERSION = os.environ.get("STRIPE_API_VERSION", "2024-06-20")
TIMEOUT = httpx.Timeout(30.0, connect=10.0)

DAY = 86400
# Stripe's shared test payment methods (no raw card data needed).
PM_GOOD = "pm_card_visa"
PM_FAIL = "pm_card_chargeCustomerFail"


def _flatten(prefix: str, value: Any, out: dict[str, str]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(f"{prefix}[{k}]", v, out)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _flatten(f"{prefix}[{i}]", v, out)
    elif value is not None:
        out[prefix] = str(value).lower() if isinstance(value, bool) else str(value)


def _encode_form(params: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in params.items():
        _flatten(key, value, out)
    return out


class Stripe:
    """Tiny test-mode Stripe REST client (self-contained; no app imports needed)."""

    def __init__(self, secret_key: str) -> None:
        if not secret_key.startswith("sk_test_"):
            raise SystemExit("refusing to run: STRIPE_SECRET_KEY must be a test key (sk_test_)")
        self._client = httpx.Client(
            base_url=STRIPE_API_BASE,
            auth=(secret_key, ""),
            timeout=TIMEOUT,
            headers={"Stripe-Version": STRIPE_API_VERSION},
        )

    def post(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self._client.post(f"/v1/{path}", data=_encode_form(params or {}))
        resp.raise_for_status()
        return resp.json()

    def get(self, path: str) -> dict[str, Any]:
        resp = self._client.get(f"/v1/{path}")
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self._client.close()


def _wait_clock_ready(stripe: Stripe, clock_id: str, timeout_s: int = 120) -> None:
    """Advancing a test clock is async; poll until it settles back to ``ready``."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        clock = stripe.get(f"test_helpers/test_clocks/{clock_id}")
        if clock["status"] == "ready":
            return
        if clock["status"] == "internal_failure":
            raise SystemExit(f"test clock {clock_id} failed to advance")
        time.sleep(2)
    raise SystemExit(f"test clock {clock_id} did not become ready within {timeout_s}s")


def _expect(label: str, actual: str, expected: str) -> None:
    mark = "✓" if actual == expected else "✗"
    print(f"  {mark} {label}: status={actual!r} (expected {expected!r})")
    if actual != expected:
        raise SystemExit(f"FAIL at step '{label}': got {actual!r}, expected {expected!r}")


def run() -> None:
    secret = os.environ.get("STRIPE_SECRET_KEY", "")
    price_id = os.environ.get("STRIPE_PRICE_ID", "")
    if not secret or not price_id:
        raise SystemExit("set STRIPE_SECRET_KEY (sk_test_...) and STRIPE_PRICE_ID env vars")

    stripe = Stripe(secret)
    try:
        now = int(time.time())

        print("1. Create test clock (frozen at now)")
        clock = stripe.post("test_helpers/test_clocks", {"frozen_time": now})
        clock_id = clock["id"]
        print(f"   clock={clock_id}")

        print("2. Create customer on the clock + attach a working card")
        customer = stripe.post(
            "customers",
            {"test_clock": clock_id, "email": f"tc-{now}@example.com", "name": "Test Clock"},
        )
        cus = customer["id"]
        cus_path = f"customers/{cus}"
        stripe.post(f"payment_methods/{PM_GOOD}/attach", {"customer": cus})
        stripe.post(cus_path, {"invoice_settings[default_payment_method]": PM_GOOD})

        print("3. Subscribe with a 14-day trial → trialing")
        sub = stripe.post(
            "subscriptions",
            {
                "customer": cus,
                "items": [{"price": price_id, "quantity": 1}],
                "trial_period_days": 14,
                "metadata": {"workspace_id": "wrk_testclock"},
            },
        )
        sub_id = sub["id"]
        item_id = sub["items"]["data"][0]["id"]
        _expect("trial", sub["status"], "trialing")

        print("4. Advance past trial end (now + 15 days) → active")
        stripe.post(f"test_helpers/test_clocks/{clock_id}/advance", {"frozen_time": now + 15 * DAY})
        _wait_clock_ready(stripe, clock_id)
        sub = stripe.get(f"subscriptions/{sub_id}")
        _expect("subscribe", sub["status"], "active")

        print("5. Seat add: update quantity 1 → 2")
        stripe.post(f"subscription_items/{item_id}", {"quantity": 2})
        sub = stripe.get(f"subscriptions/{sub_id}")
        qty = sub["items"]["data"][0]["quantity"]
        print(f"   {'✓' if qty == 2 else '✗'} seat quantity: {qty} (expected 2)")
        if qty != 2:
            raise SystemExit("FAIL: seat quantity did not update")

        print("6. Payment fail: switch to a failing card, advance a full cycle → past_due")
        stripe.post(f"payment_methods/{PM_FAIL}/attach", {"customer": cus})
        stripe.post(cus_path, {"invoice_settings[default_payment_method]": PM_FAIL})
        stripe.post(f"test_helpers/test_clocks/{clock_id}/advance", {"frozen_time": now + 46 * DAY})
        _wait_clock_ready(stripe, clock_id)
        sub = stripe.get(f"subscriptions/{sub_id}")
        _expect("payment_fail", sub["status"], "past_due")

        print("7. Recovery: restore a working card, pay the open invoice → active")
        stripe.post(cus_path, {"invoice_settings[default_payment_method]": PM_GOOD})
        invoices = stripe.get(f"invoices?subscription={sub_id}&status=open&limit=1")
        open_invoices = invoices.get("data", [])
        if open_invoices:
            stripe.post(f"invoices/{open_invoices[0]['id']}/pay", {})
        sub = stripe.get(f"subscriptions/{sub_id}")
        _expect("recovery", sub["status"], "active")

        print(f"\nAll lifecycle transitions verified ✓  (clock={clock_id}, subscription={sub_id})")
        print("Tip: with `stripe listen --forward-to localhost:8000/v0/billing/webhook` running,")
        print("     the app's /v0/billing/subscription reflects each of these states in parallel.")
    finally:
        stripe.close()


if __name__ == "__main__":
    try:
        run()
    except httpx.HTTPStatusError as exc:  # surface Stripe's error body, not just the status
        body = exc.response.text
        print(f"\nStripe API error {exc.response.status_code}: {body}", file=sys.stderr)
        sys.exit(1)
