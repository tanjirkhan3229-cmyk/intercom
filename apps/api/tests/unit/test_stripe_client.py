"""Unit tests for the Stripe REST wrapper (RFC-002 §5.6). No DB, no network."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx
import pytest

from relay.core.errors import ValidationError
from relay.modules.billing.stripe_client import (
    _encode_form,
    _headers,
    _is_retryable,
    _stripe_retry,
    verify_and_parse_event,
)
from relay.settings import Settings

SECRET = "whsec_test_secret"


def _sign(payload: bytes, *, secret: str = SECRET, timestamp: int | None = None) -> str:
    ts = timestamp if timestamp is not None else int(time.time())
    signed_payload = f"{ts}.".encode() + payload
    sig = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def test_verify_and_parse_event_accepts_valid_signature() -> None:
    payload = json.dumps({"id": "evt_1", "type": "customer.subscription.created"}).encode()
    header = _sign(payload)
    event = verify_and_parse_event(payload=payload, sig_header=header, webhook_secret=SECRET)
    assert event["id"] == "evt_1"


def test_verify_and_parse_event_rejects_bad_signature() -> None:
    payload = json.dumps({"id": "evt_1"}).encode()
    header = _sign(payload, secret="wrong_secret")
    with pytest.raises(ValidationError):
        verify_and_parse_event(payload=payload, sig_header=header, webhook_secret=SECRET)


def test_verify_and_parse_event_rejects_tampered_payload() -> None:
    payload = json.dumps({"id": "evt_1"}).encode()
    header = _sign(payload)
    tampered = json.dumps({"id": "evt_2"}).encode()
    with pytest.raises(ValidationError):
        verify_and_parse_event(payload=tampered, sig_header=header, webhook_secret=SECRET)


def test_verify_and_parse_event_rejects_expired_timestamp() -> None:
    payload = json.dumps({"id": "evt_1"}).encode()
    header = _sign(payload, timestamp=int(time.time()) - 3600)
    with pytest.raises(ValidationError):
        verify_and_parse_event(payload=payload, sig_header=header, webhook_secret=SECRET)


def test_verify_and_parse_event_rejects_missing_header() -> None:
    payload = b"{}"
    with pytest.raises(ValidationError):
        verify_and_parse_event(payload=payload, sig_header=None, webhook_secret=SECRET)


def test_verify_and_parse_event_rejects_malformed_header() -> None:
    payload = b"{}"
    with pytest.raises(ValidationError):
        verify_and_parse_event(
            payload=payload, sig_header="not-a-real-header", webhook_secret=SECRET
        )


def test_encode_form_flattens_nested_structures() -> None:
    out: dict[str, str] = {}
    from relay.modules.billing.stripe_client import _flatten

    _flatten(
        "line_items",
        [{"price": "price_123", "quantity": 1}],
        out,
    )
    assert out["line_items[0][price]"] == "price_123"
    assert out["line_items[0][quantity]"] == "1"


def test_encode_form_encodes_booleans_lowercase() -> None:
    out = _encode_form({"metadata": {"active": True}})
    assert out["metadata[active]"] == "true"


def test_encode_form_skips_none_values() -> None:
    out = _encode_form({"a": "x", "b": None})
    assert out == {"a": "x"}


# --- Multiple v1 signatures (secret rotation) -----------------------------------------------


def test_verify_accepts_when_one_of_multiple_v1_matches() -> None:
    payload = json.dumps({"id": "evt_1"}).encode()
    ts = int(time.time())
    good = hmac.new(SECRET.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    header = f"t={ts},v1=deadbeef,v1={good}"  # first is wrong, second is right
    event = verify_and_parse_event(payload=payload, sig_header=header, webhook_secret=SECRET)
    assert event["id"] == "evt_1"


# --- Outbound provider discipline: pinned version + idempotency key -------------------------


def test_headers_pin_api_version_and_carry_idempotency_key() -> None:
    headers = _headers(api_version="2024-06-20", idempotency_key="abc123")
    assert headers["Stripe-Version"] == "2024-06-20"
    assert headers["Idempotency-Key"] == "abc123"


def test_headers_omit_idempotency_key_when_none() -> None:
    headers = _headers(api_version="2024-06-20", idempotency_key=None)
    assert "Idempotency-Key" not in headers


async def test_update_subscription_item_uses_deterministic_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from relay.modules.billing.stripe_client import StripeClient

    client = StripeClient(Settings())
    captured: dict[str, object] = {}

    async def fake_post(path: str, params: dict, *, idempotency_key: str) -> dict:
        captured["path"] = path
        captured["idempotency_key"] = idempotency_key
        return {"id": "si_x", "quantity": params["quantity"]}

    monkeypatch.setattr(client, "_post", fake_post)
    await client.update_subscription_item_quantity(subscription_item_id="si_x", quantity=5)
    # Stable across crash-retries within Stripe's 24h idempotency window.
    assert captured["idempotency_key"] == "seat-sync:si_x:5"
    assert captured["path"] == "subscription_items/si_x"


# --- Bounded, jittered, transient-only retry ------------------------------------------------


def _http_status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://api.stripe.com/v1/x")
    return httpx.HTTPStatusError(str(code), request=req, response=httpx.Response(code, request=req))


def test_is_retryable_classifies_transient_vs_permanent() -> None:
    assert _is_retryable(httpx.ConnectError("boom")) is True
    assert _is_retryable(httpx.ReadTimeout("slow")) is True
    assert _is_retryable(_http_status_error(429)) is True
    assert _is_retryable(_http_status_error(500)) is True
    assert _is_retryable(_http_status_error(400)) is False  # bad request never retried
    assert _is_retryable(_http_status_error(402)) is False  # card declined never retried
    assert _is_retryable(ValueError("nope")) is False


def test_stripe_retry_retries_transient_then_succeeds() -> None:
    calls = {"n": 0}

    @_stripe_retry
    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("boom")
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 3  # bounded at _MAX_ATTEMPTS=3


def test_stripe_retry_does_not_retry_permanent_error() -> None:
    calls = {"n": 0}

    @_stripe_retry
    def bad() -> None:
        calls["n"] += 1
        raise _http_status_error(400)

    with pytest.raises(httpx.HTTPStatusError):
        bad()
    assert calls["n"] == 1  # 4xx surfaces immediately, no retry
