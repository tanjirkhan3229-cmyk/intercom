"""PII / secret scrubbing (RFC-001 §10: "PII minimization in logs").

One redaction pass reused by both structlog (every log line) and Sentry (``before_send``). Two
layers, because secrets leak both ways:

- **key-based**: values under a sensitive-looking key are dropped wholesale;
- **value-based**: emails, JWTs, ``relaysk_`` API keys and ``Bearer <token>`` fragments embedded
  in *any* string (exception messages, free-text log fields, Sentry ``exception.value``) are masked
  even when the key is innocuous.

The scrubber over-redacts rather than risk leaking a secret, but key matching is tuned so it does
not destroy benign numeric fields like ``token_count`` / ``session_count``.
"""

from __future__ import annotations

import re
from collections.abc import MutableMapping
from typing import Any

REDACTED = "***"

# Substrings that unambiguously mark a key's value as secret/PII (safe to over-match).
_SENSITIVE_SUBSTRINGS: frozenset[str] = frozenset(
    {
        "authorization",
        "cookie",
        "password",
        "passwd",
        "passphrase",
        "secret",
        "api_key",
        "apikey",
        "api-key",
        "key_hash",
        "signing_key",
        "encryption_key",
        "user_hash",
        "hmac",
        "credential",
        "private_key",
    }
)
# Words that are sensitive only as a WHOLE trailing segment, so ``access_token`` / ``id_token`` /
# ``user_session`` redact but the metric fields ``token_count`` / ``session_count`` do not.
_SENSITIVE_SUFFIX_WORDS: tuple[str, ...] = ("token", "session", "jwt")

# Value-level secret patterns (masked wherever they appear in a string). Label lengths are bounded
# (RFC-5321 local<=64, domain<=255) so re.sub stays LINEAR — an unbounded ``+`` here backtracks
# O(n^2) on a long non-matching run, turning a big attacker-influenced log field into CPU burn.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,255}\.[A-Za-z]{2,24}")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_API_KEY_RE = re.compile(r"\brelaysk_[A-Za-z0-9_]+")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]+=*")


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    if any(part in lowered for part in _SENSITIVE_SUBSTRINGS):
        return True
    return any(
        lowered == word or lowered.endswith(f"_{word}") or lowered.endswith(f"-{word}")
        for word in _SENSITIVE_SUFFIX_WORDS
    )


def _redact_secrets(value: str) -> str:
    """Mask secrets embedded in a free-text string (order: bearer → jwt → api key → email)."""
    value = _BEARER_RE.sub(REDACTED, value)
    value = _JWT_RE.sub(REDACTED, value)
    value = _API_KEY_RE.sub(REDACTED, value)
    return _EMAIL_RE.sub(REDACTED, value)


def scrub(obj: Any, *, key: str | None = None) -> Any:
    """Recursively redact ``obj``. Sensitive-keyed values are dropped whole; strings/bytes have
    embedded secrets masked; dicts/lists/tuples/sets are walked."""
    if key is not None and _is_sensitive_key(key):
        return REDACTED
    if isinstance(obj, MutableMapping):
        return {k: scrub(v, key=str(k)) for k, v in obj.items()}
    if isinstance(obj, dict):
        return {k: scrub(v, key=str(k)) for k, v in obj.items()}
    if isinstance(obj, (set, frozenset)):
        # Serialize to a list: a scrubbed element can be an unhashable container (e.g. a scrubbed
        # nested frozenset becomes a list), which would break a set comprehension. Order/type are
        # irrelevant for log/Sentry payloads.
        return [scrub(v) for v in obj]
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(scrub(v) for v in obj)
    if isinstance(obj, str):
        return _redact_secrets(obj)
    if isinstance(obj, (bytes, bytearray)):
        return _redact_secrets(obj.decode("utf-8", "replace"))
    return obj


def scrub_processor(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: scrub the whole event dict before it is rendered."""
    scrubbed = scrub(dict(event_dict))
    assert isinstance(scrubbed, dict)  # scrub of a dict always returns a dict
    return scrubbed


def sentry_before_send(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any]:
    """Sentry ``before_send`` hook: scrub the outgoing event (headers, extra, message, ...)."""
    scrubbed = scrub(event)
    assert isinstance(scrubbed, dict)
    return scrubbed
