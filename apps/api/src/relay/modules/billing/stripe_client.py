"""Minimal Stripe REST client (RFC-001 §5 — no blocking calls in ``async def`` paths).

The official ``stripe`` SDK is sync-only, so a thin ``httpx``-based wrapper is used instead:
the async client uses a real ``await`` for request-path calls, the sync helper serves the
Celery ``workers`` shape, and both are trivial to fake in tests (swap in an object with the
same method signatures — no network, no SDK monkeypatching). Auth is HTTP Basic with the
secret key as username (Stripe's convention); bodies are form-encoded using Stripe's bracket
notation for nested params (``_flatten``).

Provider discipline (RFC-001 §5, master rule 5) — every outbound call:
- pins the Stripe API version (``Stripe-Version`` header) so provider upgrades can't silently
  reshape payloads;
- carries an ``Idempotency-Key`` on mutating POSTs, so a network retry can never double-create
  a checkout/portal session or double-apply a quantity change;
- has a bounded timeout and a bounded, **jittered** retry that fires only on transient failures
  (network errors, 429, 5xx) — safe precisely because every mutating call is idempotency-keyed.

Webhook signature verification (:func:`verify_and_parse_event`) reimplements Stripe's
documented scheme (HMAC-SHA256 over ``"{timestamp}.{payload}"``) rather than depend on the
SDK — see https://stripe.com/docs/webhooks/signatures. It enforces a timestamp tolerance
(replay-window) and compares in constant time.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from relay.core.errors import ValidationError
from relay.core.ids import uuid7_str
from relay.settings import Settings

# Every external call is timed out (master rule 5).
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
# Reject a webhook whose timestamp is older than this (replay-attack window).
_WEBHOOK_TOLERANCE_SECONDS = 300
# Bounded retry budget for transient Stripe failures (idempotency-keyed, so retry-safe).
_MAX_ATTEMPTS = 3


def _is_retryable(exc: BaseException) -> bool:
    """Transient-only: network/timeout errors and Stripe 429/5xx. A 4xx (bad request, card
    declined, auth) is never retried — it will only fail again."""
    if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


# Bounded attempts + exponential backoff with jitter (master rule 5 / RFC-001 §5). ``reraise``
# so the final underlying error surfaces to the caller rather than a RetryError wrapper.
_stripe_retry = retry(
    reraise=True,
    stop=stop_after_attempt(_MAX_ATTEMPTS),
    wait=wait_exponential_jitter(initial=0.2, max=5.0),
    retry=retry_if_exception(_is_retryable),
)


def _flatten(prefix: str, value: Any, out: dict[str, str]) -> None:
    """Encode a nested dict/list into Stripe's ``a[b][0][c]`` form-field notation."""
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


def _headers(*, api_version: str, idempotency_key: str | None) -> dict[str, str]:
    headers = {"Stripe-Version": api_version}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


class StripeClient:
    """Async wrapper over the subset of the Stripe API billing needs."""

    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.stripe_api_base
        self._secret_key = settings.stripe_secret_key
        self._api_version = settings.stripe_api_version

    @_stripe_retry
    async def _post(
        self, path: str, params: dict[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]:
        # ``idempotency_key`` is a parameter (not generated inside) so it stays STABLE across
        # tenacity retry attempts — that is what makes the retry safe.
        headers = _headers(api_version=self._api_version, idempotency_key=idempotency_key)
        async with httpx.AsyncClient(
            base_url=self._base_url, auth=(self._secret_key, ""), timeout=_TIMEOUT
        ) as client:
            resp = await client.post(f"/v1/{path}", data=_encode_form(params), headers=headers)
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]

    async def create_checkout_session(
        self,
        *,
        price_id: str,
        customer_email: str,
        workspace_id: str,
        trial_days: int,
        success_url: str,
        cancel_url: str,
    ) -> dict[str, Any]:
        """Stripe Checkout, subscription mode, with a trial. Stripe creates the customer."""
        return await self._post(
            "checkout/sessions",
            {
                "mode": "subscription",
                "customer_email": customer_email,
                "client_reference_id": workspace_id,
                "success_url": success_url,
                "cancel_url": cancel_url,
                "line_items": [{"price": price_id, "quantity": 1}],
                "subscription_data": {
                    "trial_period_days": trial_days,
                    "metadata": {"workspace_id": workspace_id},
                },
                "metadata": {"workspace_id": workspace_id},
            },
            idempotency_key=uuid7_str(),
        )

    async def create_portal_session(self, *, customer_id: str, return_url: str) -> dict[str, Any]:
        return await self._post(
            "billing_portal/sessions",
            {"customer": customer_id, "return_url": return_url},
            idempotency_key=uuid7_str(),
        )

    async def update_subscription_item_quantity(
        self, *, subscription_item_id: str, quantity: int
    ) -> dict[str, Any]:
        # Deterministic key: a crash-retry that re-sends the same target quantity reuses the
        # same key (Stripe replays the original response within its 24h window) rather than
        # racing a second update.
        return await self._post(
            f"subscription_items/{subscription_item_id}",
            {"quantity": quantity},
            idempotency_key=f"seat-sync:{subscription_item_id}:{quantity}",
        )


@_stripe_retry
def update_subscription_item_quantity_sync(
    *, settings: Settings, subscription_item_id: str, quantity: int
) -> None:
    """Sync counterpart for the Celery ``workers`` runtime shape (RFC-001 §6.1) — tasks run
    synchronously, so a blocking call here is correct (unlike in an ``async def`` route).

    Same provider discipline as the async client: pinned API version, deterministic
    idempotency key (stable across crash-retries), bounded jittered retry via the shared
    ``_stripe_retry`` decorator."""
    auth = (settings.stripe_secret_key, "")
    headers = _headers(
        api_version=settings.stripe_api_version,
        idempotency_key=f"seat-sync:{subscription_item_id}:{quantity}",
    )
    with httpx.Client(base_url=settings.stripe_api_base, auth=auth, timeout=_TIMEOUT) as client:
        resp = client.post(
            f"/v1/subscription_items/{subscription_item_id}",
            data=_encode_form({"quantity": quantity}),
            headers=headers,
        )
        resp.raise_for_status()


@_stripe_retry
def create_meter_event_sync(
    *,
    settings: Settings,
    event_name: str,
    stripe_customer_id: str,
    value: int,
    identifier: str,
) -> None:
    """Report one usage delta to a Stripe Billing Meter (P1.3, RFC-002 §5.6 async metering).

    Billing Meters aggregate events by ``event_name`` + customer — no per-item quantity to keep in
    sync, unlike seats. ``identifier`` is Stripe's native dedupe key (a redelivered event with the
    same identifier is ignored), so re-running the sync task is safe; the HTTP ``Idempotency-Key``
    is the same value for transport-level retry safety. ``value`` may be negative (a resolution
    claw-back) — billing meters accept negative-value events against a ``sum`` aggregation."""
    auth = (settings.stripe_secret_key, "")
    headers = _headers(
        api_version=settings.stripe_api_version, idempotency_key=f"meter:{identifier}"
    )
    params = {
        "event_name": event_name,
        "identifier": identifier,
        "payload": {"stripe_customer_id": stripe_customer_id, "value": str(value)},
    }
    with httpx.Client(base_url=settings.stripe_api_base, auth=auth, timeout=_TIMEOUT) as client:
        resp = client.post("/v1/billing/meter_events", data=_encode_form(params), headers=headers)
        resp.raise_for_status()


def verify_and_parse_event(
    *, payload: bytes, sig_header: str | None, webhook_secret: str
) -> dict[str, Any]:
    """Verify the ``Stripe-Signature`` header and return the parsed event JSON.

    Raises :class:`ValidationError` (mapped to 422) on a missing/malformed header, a
    signature mismatch, or a timestamp outside the replay-attack tolerance window. Handles
    multiple ``v1=`` signatures (Stripe emits several during secret rotation) — a match
    against any one passes, each compared in constant time.
    """
    import orjson

    if not sig_header:
        raise ValidationError("missing Stripe-Signature header")

    timestamp: str | None = None
    signatures: list[str] = []
    for item in sig_header.split(","):
        k, _, v = item.partition("=")
        if k == "t":
            timestamp = v
        elif k == "v1":
            signatures.append(v)
    if not timestamp or not signatures:
        raise ValidationError("malformed Stripe-Signature header")

    if abs(time.time() - int(timestamp)) > _WEBHOOK_TOLERANCE_SECONDS:
        raise ValidationError("Stripe webhook timestamp outside tolerance")

    signed_payload = f"{timestamp}.".encode() + payload
    expected = hmac.new(webhook_secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
        raise ValidationError("Stripe webhook signature mismatch")

    return orjson.loads(payload)  # type: ignore[no-any-return]
