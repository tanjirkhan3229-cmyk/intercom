"""Stateless one-click unsubscribe tokens (RFC 8058; P1.8).

A marketing email carries ``List-Unsubscribe: <https://…/v0/outbound/u/{token}>`` plus
``List-Unsubscribe-Post: List-Unsubscribe=One-Click``. The token is a **stateless** HMAC over
``workspace_id || contact_id || subscription_type_id || expiry`` — no DB row, so the public
endpoint routes the opt-out to its workspace with zero lookups and no pre-tenancy RLS problem.

Forgery requires the dedicated ``unsubscribe_token_secret`` (separate from the reply/JWT keys so
audiences never cross); verification is constant-time. Expiry is generous (~400 days) purely to
bound replay of a leaked link — a legitimate late click on an old email must still work, and
unsubscribe is idempotent anyway. A forged/expired token yields ``None`` (the endpoint then shows a
neutral page — no validity oracle).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import struct
import time
import uuid

from relay.settings import get_settings

_SIG_BYTES = 10
_PAYLOAD_BYTES = 52  # three raw uuids (48) + u32 expiry (4)
_DEFAULT_TTL_SECONDS = 400 * 24 * 3600  # ~400 days


def _key() -> bytes:
    return get_settings().unsubscribe_token_secret.encode("utf-8")


def make_unsubscribe_token(
    workspace_id: uuid.UUID,
    contact_id: uuid.UUID,
    subscription_type_id: uuid.UUID,
    *,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> str:
    """Return the opaque token embedded in the List-Unsubscribe URL for one (contact, type)."""
    exp = int(now if now is not None else time.time()) + ttl_seconds
    payload = (
        workspace_id.bytes
        + contact_id.bytes
        + subscription_type_id.bytes
        + struct.pack(">I", exp & 0xFFFFFFFF)
    )
    sig = hmac.new(_key(), payload, hashlib.sha256).digest()[:_SIG_BYTES]
    return base64.urlsafe_b64encode(payload + sig).decode("ascii").rstrip("=")


def parse_unsubscribe_token(
    token: str, *, now: float | None = None
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID] | None:
    """Verify a token; return ``(workspace_id, contact_id, subscription_type_id)`` or ``None``.

    ``None`` on any of: malformed encoding, wrong length, bad signature, or expiry in the past.
    """
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (binascii.Error, ValueError):
        return None
    if len(raw) != _PAYLOAD_BYTES + _SIG_BYTES:
        return None
    payload, sig = raw[:_PAYLOAD_BYTES], raw[_PAYLOAD_BYTES:]
    expected = hmac.new(_key(), payload, hashlib.sha256).digest()[:_SIG_BYTES]
    if not hmac.compare_digest(sig, expected):
        return None
    (exp,) = struct.unpack(">I", payload[48:52])
    if exp < int(now if now is not None else time.time()):
        return None
    return (
        uuid.UUID(bytes=payload[0:16]),
        uuid.UUID(bytes=payload[16:32]),
        uuid.UUID(bytes=payload[32:48]),
    )
