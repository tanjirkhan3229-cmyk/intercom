"""Security primitives (RFC-001 §10): password hashing, JWTs, token/secret handling.

- Passwords: argon2id (argon2-cffi).
- Access tokens: short-lived (15 min) signed JWTs (HS256) carrying workspace + role.
- Opaque secrets (refresh tokens, API keys): generated with the CSPRNG; only their
  SHA-256 hash is stored, so a DB leak never yields a usable credential.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
import uuid
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from relay.settings import get_settings

_hasher = PasswordHasher()

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_TYPE = "access"
WIDGET_SESSION_TYPE = "widget"


# --- Passwords ----------------------------------------------------------------


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time-ish verify. Returns False on mismatch or malformed hash."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


# --- Opaque secrets (refresh tokens, API keys) --------------------------------


def generate_secret(nbytes: int = 32) -> str:
    """URL-safe high-entropy secret."""
    return secrets.token_urlsafe(nbytes)


def hash_secret(value: str) -> str:
    """SHA-256 hex of an opaque secret. Deterministic → indexable for lookup."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def compare_secret(value: str, stored_hash: str) -> bool:
    return secrets.compare_digest(hash_secret(value), stored_hash)


# --- Access JWTs --------------------------------------------------------------


def create_access_token(
    *,
    admin_id: uuid.UUID,
    workspace_id: uuid.UUID,
    role: str,
    now: dt.datetime | None = None,
) -> str:
    settings = get_settings()
    issued = now or dt.datetime.now(dt.UTC)
    expires = issued + dt.timedelta(seconds=settings.access_token_ttl_seconds)
    payload: dict[str, Any] = {
        "sub": str(admin_id),
        "ws": str(workspace_id),
        "role": role,
        "type": ACCESS_TOKEN_TYPE,
        "iat": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.jwt_signing_key, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode + validate an access token. Raises ``jwt.PyJWTError`` on any problem."""
    settings = get_settings()
    payload: dict[str, Any] = jwt.decode(
        token,
        settings.jwt_signing_key,
        algorithms=[JWT_ALGORITHM],
        options={"require": ["exp", "iat", "sub", "ws"]},
    )
    if payload.get("type") != ACCESS_TOKEN_TYPE:
        raise jwt.InvalidTokenError("wrong token type")
    return payload


# --- Widget (end-user/contact) session JWTs -----------------------------------


def create_widget_session_token(
    *, contact_id: uuid.UUID, workspace_id: uuid.UUID, now: dt.datetime | None = None
) -> str:
    """A widget contact's session JWT. Longer-lived than an agent access token (a lead keeps
    its session across visits) but low-privilege: it only ever authorises the contact's own
    conversations, and RLS scopes every read to ``ws``. Signed with ``jwt_signing_key``; the
    ``type`` claim keeps it disjoint from agent access tokens."""
    settings = get_settings()
    issued = now or dt.datetime.now(dt.UTC)
    expires = issued + dt.timedelta(seconds=settings.widget_session_ttl_seconds)
    payload: dict[str, Any] = {
        "sub": str(contact_id),
        "ws": str(workspace_id),
        "type": WIDGET_SESSION_TYPE,
        "iat": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.jwt_signing_key, algorithm=JWT_ALGORITHM)


def decode_widget_session_token(token: str) -> dict[str, Any]:
    """Decode + validate a widget session token. Raises ``jwt.PyJWTError`` on any problem."""
    settings = get_settings()
    payload: dict[str, Any] = jwt.decode(
        token,
        settings.jwt_signing_key,
        algorithms=[JWT_ALGORITHM],
        options={"require": ["exp", "iat", "sub", "ws"]},
    )
    if payload.get("type") != WIDGET_SESSION_TYPE:
        raise jwt.InvalidTokenError("wrong token type")
    return payload


# --- Messenger identity verification (HMAC, RFC-001 §10) ----------------------


def compute_identity_hash(secret: str, external_id: str) -> str:
    """HMAC-SHA256 of the tenant's own user id under the per-workspace secret — the value the
    customer's backend computes and passes to ``relay('boot', { user_hash })``."""
    return hmac.new(secret.encode("utf-8"), external_id.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_identity_hash(secret: str, external_id: str, user_hash: str) -> bool:
    """Constant-time compare of a client-supplied ``user_hash`` against the expected HMAC."""
    return hmac.compare_digest(compute_identity_hash(secret, external_id), user_hash)
