"""Slack request-signature verification (P1.9 reply-from-Slack).

Slack signs every request to our Events endpoint: the base string is ``v0:{timestamp}:{raw_body}``
HMAC-SHA256'd with the app's *signing secret*, sent as ``X-Slack-Signature: v0=<hex>`` plus
``X-Slack-Request-Timestamp``. We must verify over the EXACT raw request bytes (re-serialising the
JSON would change them and break the HMAC), constant-time, within a replay window. Pure functions,
so tests assert compute/verify agree and that tampering / an expired timestamp fail.
"""

from __future__ import annotations

import hashlib
import hmac

SIGNATURE_HEADER = "X-Slack-Signature"
TIMESTAMP_HEADER = "X-Slack-Request-Timestamp"
_SCHEME = "v0"


def compute_signature(signing_secret: str, timestamp: int, body: bytes) -> str:
    """Return the ``X-Slack-Signature`` value (``v0=<hex>``) for ``body`` at ``timestamp``."""
    base = f"{_SCHEME}:{timestamp}:".encode() + body
    mac = hmac.new(signing_secret.encode("utf-8"), base, hashlib.sha256)
    return f"{_SCHEME}={mac.hexdigest()}"


def verify_signature(
    signing_secret: str,
    *,
    timestamp: int,
    body: bytes,
    header: str,
    tolerance_seconds: int,
    now: int,
) -> bool:
    """Constant-time verify + replay-window check. Never raises; returns False on any mismatch,
    an out-of-window timestamp, or a malformed header."""
    if not header or abs(now - timestamp) > tolerance_seconds:
        return False
    expected = compute_signature(signing_secret, timestamp, body)
    return hmac.compare_digest(expected, header)
