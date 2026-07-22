"""Google OIDC helper (RFC-001 §10).

Authorization-code flow with PKCE-less state/nonce (confidential client). Google's endpoints
are stable constants (skips a discovery round-trip). The security-critical step —
verifying the ``id_token`` signature against Google's JWKS and checking aud/iss/nonce — is
done with PyJWT. Every network call has a timeout.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWKClient

from relay.core.errors import AppError
from relay.settings import get_settings

AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
HTTP_TIMEOUT = 10.0

_jwks_client = PyJWKClient(JWKS_URI)


class OIDCDisabledError(AppError):
    status_code = 400
    code = "oidc_disabled"


class OIDCError(AppError):
    status_code = 401
    code = "oidc_error"


@dataclass
class OIDCIdentity:
    sub: str
    email: str
    name: str
    email_verified: bool


def _require_config() -> tuple[str, str, str]:
    settings = get_settings()
    if not settings.google_oidc_enabled or not settings.google_oidc_redirect_uri:
        raise OIDCDisabledError("Google OIDC is not configured")
    assert settings.google_oidc_client_id and settings.google_oidc_client_secret
    return (
        settings.google_oidc_client_id,
        settings.google_oidc_client_secret,
        settings.google_oidc_redirect_uri,
    )


def new_state() -> str:
    return secrets.token_urlsafe(24)


def new_nonce() -> str:
    return secrets.token_urlsafe(24)


def build_authorization_url(*, state: str, nonce: str) -> str:
    client_id, _secret, redirect_uri = _require_config()
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "access_type": "online",
        "prompt": "select_account",
    }
    return f"{AUTHORIZATION_ENDPOINT}?{urlencode(params)}"


async def exchange_code(code: str) -> dict[str, Any]:
    client_id, client_secret, redirect_uri = _require_config()
    data = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(TOKEN_ENDPOINT, data=data)
    if resp.status_code != 200:
        raise OIDCError("token exchange failed")
    tokens: dict[str, Any] = resp.json()
    return tokens


def verify_id_token(id_token: str, *, expected_nonce: str) -> OIDCIdentity:
    client_id, _secret, _redirect = _require_config()
    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=list(ISSUERS),
            options={"require": ["exp", "iat", "aud", "iss", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise OIDCError("invalid id_token") from exc

    if claims.get("nonce") != expected_nonce:
        raise OIDCError("nonce mismatch")

    email = claims.get("email")
    if not email:
        raise OIDCError("id_token missing email")
    return OIDCIdentity(
        sub=str(claims["sub"]),
        email=str(email),
        name=str(claims.get("name") or email),
        email_verified=bool(claims.get("email_verified", False)),
    )
