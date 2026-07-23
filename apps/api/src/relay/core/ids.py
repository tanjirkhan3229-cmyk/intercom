"""Identifier helpers (RFC-002 §5.1).

- **UUIDv7** primary keys, app-generated: time-ordered so inserts stay index-local on
  write-heavy tables (conversation_parts, events, ...).
- **Prefixed base62 public IDs** (``wrk_``, ``adm_`` ...): what we expose over the API.
  Internally we always store the raw ``uuid``; the prefix + base62 is a presentation
  encoding that round-trips losslessly.

No third-party dependency: uuid7 is a ~15-line construction over the RFC 9562 layout.
"""

from __future__ import annotations

import os
import time
import uuid

# --- UUIDv7 --------------------------------------------------------------------

# Monotonic guard so two UUIDs minted in the same millisecond keep their order.
_last_ms = 0
_last_uuid_int = 0


def uuid7() -> uuid.UUID:
    """Return a UUIDv7 (RFC 9562): 48-bit unix-ms timestamp + version/variant + random.

    Values are time-ordered for index locality and monotonic within a process even
    when several are generated in the same millisecond.
    """
    global _last_ms, _last_uuid_int

    ms = time.time_ns() // 1_000_000
    if ms <= _last_ms:
        # Same (or clock-regressed) millisecond: increment the previous value so
        # ordering is preserved without waiting for the clock to advance.
        _last_uuid_int = (_last_uuid_int + 1) & ((1 << 128) - 1)
        return uuid.UUID(int=_last_uuid_int)

    rand = int.from_bytes(os.urandom(10), "big")  # 80 random bits; we use 74
    rand_a = rand & 0xFFF  # 12 bits
    rand_b = (rand >> 12) & ((1 << 62) - 1)  # 62 bits

    value = (ms & 0xFFFFFFFFFFFF) << 80
    value |= 0x7 << 76  # version 7
    value |= rand_a << 64
    value |= 0x2 << 62  # variant (10xx)
    value |= rand_b

    _last_ms = ms
    _last_uuid_int = value
    return uuid.UUID(int=value)


def uuid7_str() -> str:
    return str(uuid7())


# --- Base62 prefixed public IDs ------------------------------------------------

_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_BASE = len(_ALPHABET)
_INDEX = {c: i for i, c in enumerate(_ALPHABET)}


def _b62_encode(n: int) -> str:
    if n == 0:
        return _ALPHABET[0]
    out: list[str] = []
    while n:
        n, rem = divmod(n, _BASE)
        out.append(_ALPHABET[rem])
    return "".join(reversed(out))


def _b62_decode(s: str) -> int:
    n = 0
    for ch in s:
        n = n * _BASE + _INDEX[ch]
    return n


def encode_public_id(prefix: str, value: uuid.UUID) -> str:
    """``uuid`` -> ``"<prefix>_<base62>"`` (e.g. ``wrk_2Yx...``)."""
    return f"{prefix}_{_b62_encode(value.int)}"


def decode_public_id(prefix: str, public_id: str) -> uuid.UUID:
    """Inverse of :func:`encode_public_id`. Raises ``ValueError`` on prefix mismatch."""
    expected = f"{prefix}_"
    if not public_id.startswith(expected):
        raise ValueError(f"expected id with prefix {prefix!r}, got {public_id!r}")
    body = public_id[len(expected) :]
    return uuid.UUID(int=_b62_decode(body))


# Canonical prefixes per resource (RFC-002 §5.1). Extend as modules land.
class IdPrefix:
    WORKSPACE = "wrk"
    ADMIN = "adm"
    MEMBERSHIP = "mem"
    TEAM = "team"
    API_KEY = "key"
    CONTACT = "usr"
    COMPANY = "cmp"
    ATTRIBUTE = "attr"
    CONVERSATION = "cnv"
    PART = "msg"
    SAVED_REPLY = "rep"
    ARTICLE = "art"
    COLLECTION = "col"
    PLAN = "pln"
    SUBSCRIPTION = "sub"
    # channels module (P0.7 — email)
    DOMAIN = "dom"
    CHANNEL_ACCOUNT = "cha"
    SUPPRESSION = "sup"
    EMAIL_MESSAGE = "eml"
    # webhooks module (P0.11)
    WEBHOOK_SUBSCRIPTION = "whk"
    WEBHOOK_DELIVERY = "whd"
    WEBHOOK_EVENT = "evt"  # opaque public event id carried in the delivered payload
