"""Stateless plus-addressed reply tokens (RFC-001 §6.6).

Outbound email sets ``Reply-To: reply+{token}@{email_inbound_domain}``. The token is a
**stateless** HMAC over ``workspace_id || conversation_id`` — no DB row, so an inbound reply is
routed to its workspace + conversation with zero lookups (and, crucially, no pre-tenancy RLS
problem). Forgery requires the dedicated ``email_reply_token_secret`` (kept separate from the JWT
key so token audiences never cross); verification is constant-time.

The token still only *routes*: the ingest task independently authenticates the inbound sender
against the conversation's contact before appending (a leaked reply address must not let a
stranger inject a message).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import uuid

from relay.settings import get_settings

# 80-bit truncated tag — ample for a routing token whose forgery already needs the secret key.
_SIG_BYTES = 10
_PAYLOAD_BYTES = 32  # two raw uuids


def _key() -> bytes:
    return get_settings().email_reply_token_secret.encode("utf-8")


def make_reply_token(workspace_id: uuid.UUID, conversation_id: uuid.UUID) -> str:
    """Return the opaque token embedded in the plus-address of an outbound email's Reply-To."""
    payload = workspace_id.bytes + conversation_id.bytes
    sig = hmac.new(_key(), payload, hashlib.sha256).digest()[:_SIG_BYTES]
    return base64.urlsafe_b64encode(payload + sig).decode("ascii").rstrip("=")


def parse_reply_token(token: str) -> tuple[uuid.UUID, uuid.UUID] | None:
    """Verify a token and return ``(workspace_id, conversation_id)``, or ``None`` if invalid."""
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
    return (uuid.UUID(bytes=payload[:16]), uuid.UUID(bytes=payload[16:32]))


def reply_address(token: str) -> str:
    """Build the full ``reply+{token}@{inbound_domain}`` address."""
    return f"reply+{token}@{get_settings().email_inbound_domain}"
