"""HMAC-SHA256 webhook signatures (P0.11, RFC-001 §6.7).

Stripe-style scheme, chosen so customers can reuse well-known verification recipes:
- signed content is ``f"{timestamp}.".encode() + body`` — binding the signature to a moment so a
  replayed request is detectable via a freshness window;
- the header value is ``v1=<hexdigest>`` — the ``v1=`` scheme tag lets us rotate the algorithm
  later without breaking existing verifiers;
- verification is constant-time (``hmac.compare_digest``).

These pure functions are the single source of truth for the wire format: the delivery task signs
with :func:`compute_signature`, docs/webhooks/signature-verification.md documents the identical
algorithm, and tests/unit/test_webhook_signing.py asserts the two agree (and that tampering or an
expired timestamp fail).
"""

from __future__ import annotations

import hashlib
import hmac

SIGNATURE_HEADER = "Relay-Signature"
TIMESTAMP_HEADER = "Relay-Timestamp"
_SCHEME = "v1"


def signed_content(timestamp: int, body: bytes) -> bytes:
    """The exact bytes the HMAC is computed over: ``"{timestamp}." + body``."""
    return f"{timestamp}.".encode() + body


def compute_signature(secret: str, timestamp: int, body: bytes) -> str:
    """Return the ``Relay-Signature`` header value (``v1=<hex>``) for ``body`` at ``timestamp``."""
    mac = hmac.new(secret.encode("utf-8"), signed_content(timestamp, body), hashlib.sha256)
    return f"{_SCHEME}={mac.hexdigest()}"


def verify_signature(
    secret: str,
    *,
    timestamp: int,
    body: bytes,
    header: str,
    tolerance_seconds: int,
    now: int,
) -> bool:
    """Constant-time verify + replay-window check (the receiver-side algorithm, mirrored in docs).

    Returns False if the timestamp is outside ``tolerance_seconds`` of ``now`` or the signature
    does not match — never raises on a bad signature.
    """
    if abs(now - timestamp) > tolerance_seconds:
        return False
    expected = compute_signature(secret, timestamp, body)
    return hmac.compare_digest(expected, header)
