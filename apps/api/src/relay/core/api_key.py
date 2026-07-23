"""Public-API key format + parsing (P0.11, RFC-001 §10).

A key looks like ``relaysk_<workspace_public_id>_<secret>`` — e.g.
``relaysk_wrk_2Yx..._<urlsafe-secret>``. The workspace public id is embedded so the tenant is
resolvable *before* the RLS GUC is set (the same trick ``refresh_tokens`` use — see
identity/models.py). Only the SHA-256 hash of the whole key is stored (``api_keys.key_hash``).

``API_KEY_LABEL`` is the single source of truth for the key prefix: the identity service mints
keys with it and the tenancy middleware parses them with it, so the two can never drift.
"""

from __future__ import annotations

import uuid

from relay.core.ids import IdPrefix, decode_public_id

# Brand prefix for every public-API key. Single source of truth (identity mints, middleware parses).
API_KEY_LABEL = "relaysk"


def looks_like_api_key(token: str) -> bool:
    """True if ``token`` is shaped like an API key (``relaysk_…``) rather than a JWT."""
    return token.startswith(f"{API_KEY_LABEL}_")


def parse_api_key(raw_key: str) -> uuid.UUID:
    """Extract the workspace id embedded in an API key. Raises ``ValueError`` if malformed.

    Parsed by *structure*, never a naive ``split``: the secret is ``secrets.token_urlsafe``
    output and may itself contain ``_``/``-``, but the base62 alphabet (core/ids) excludes both,
    so the workspace's base62 body ends at the first ``_`` after the ``wrk_`` prefix.

    The embedded id is **untrusted** — it only tells us which workspace to pin the RLS lookup to;
    the stored key row's own ``workspace_id`` (under RLS) is the authoritative tenant.
    """
    label = f"{API_KEY_LABEL}_"
    if not raw_key.startswith(label):
        raise ValueError("not an api key")
    rest = raw_key[len(label) :]  # "wrk_<base62>_<secret>"
    ws_prefix = f"{IdPrefix.WORKSPACE}_"  # "wrk_"
    if not rest.startswith(ws_prefix):
        raise ValueError("api key missing workspace prefix")
    body = rest[len(ws_prefix) :]  # "<base62>_<secret>"
    b62, sep, secret = body.partition("_")  # base62 ends at the first "_"
    if not sep or not b62 or not secret:
        raise ValueError("malformed api key")
    try:
        return decode_public_id(IdPrefix.WORKSPACE, f"{ws_prefix}{b62}")
    except (ValueError, KeyError) as exc:  # bad base62 char → KeyError; oversized int → ValueError
        raise ValueError("malformed api key workspace id") from exc
