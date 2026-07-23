"""Symmetric encryption for secrets that must be recoverable (P0.11).

Almost every secret in Relay is one-way hashed (passwords, API keys, refresh tokens — see
core/security.py) because we only ever *compare* them. Webhook signing secrets are the exception:
the delivery worker must recover the raw secret to compute the HMAC over each payload, so a hash
won't do. We encrypt them at rest with Fernet (AES-128-CBC + HMAC-SHA256, authenticated) keyed off
``settings.secret_encryption_key`` — so a database leak alone cannot forge a customer's webhooks.

The Fernet key is *derived* from ``secret_encryption_key`` (SHA-256 → urlsafe-base64) so operators
may set any passphrase (``min_length=16``) rather than a Fernet-formatted 32-byte key.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from relay.settings import get_settings

__all__ = ["InvalidToken", "decrypt_secret", "encrypt_secret"]


def _fernet() -> Fernet:
    # Rebuilt per call (cheap) so a test that rotates ``secret_encryption_key`` + clears the
    # settings cache is honoured — no stale module-level key.
    raw = get_settings().secret_encryption_key.encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())  # 32 bytes → valid Fernet key
    return Fernet(key)


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret for storage; returns an opaque urlsafe token."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    """Recover a secret from :func:`encrypt_secret`. Raises ``InvalidToken`` if tampered or
    encrypted under a different key."""
    return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
