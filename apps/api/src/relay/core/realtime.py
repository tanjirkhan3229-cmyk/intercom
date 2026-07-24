"""Realtime gateway integration (Centrifugo — RFC-001 §6.1 gateway row, §6.3, §9).

Realtime is **bought, not built**: Centrifugo owns the 500k websockets and pub/sub fan-out.
This module is the API's thin edge onto it:

- **Channel scheme.** ``conv:{cnv_id}`` (one conversation's thread) and
  ``inbox:{wrk_id}:{team}`` (an inbox view). ``team`` is a team public id, or the reserved
  tokens ``all`` (workspace firehose) / ``none`` (unassigned). Channel names carry only public
  ids, never raw UUIDs.
- **Tokens.** Per-connection JWTs identify a client to Centrifugo; per-channel subscription
  JWTs authorise one channel. Both are HS256 signed with ``centrifugo_token_secret`` (== the
  gateway's ``token_hmac_secret_key``). Agents get an identity-only connection token and fetch
  subscription tokens per channel (authorised against their workspace in the messaging service).
  A widget contact gets a connection token whose ``channels`` claim is pinned to exactly its one
  ``conv:`` channel — so the gateway itself refuses any other channel for that connection.
- **Publish.** Server→Centrifugo over its HTTP API (``POST /api/publish`` + ``X-API-Key``). The
  realtime-fanout consumer publishes on outbox consumption; typing/presence relay inline.
- **Typing & presence.** Redis-only with TTL (RFC-002 §2 note), relayed through Centrifugo,
  never persisted to Postgres.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import httpx
import jwt

from relay.core.ids import IdPrefix, encode_public_id
from relay.core.logging import get_logger
from relay.core.redis import get_redis
from relay.settings import get_settings

log = get_logger(__name__)

_ALG = "HS256"

# Ephemeral state lives in Redis with a TTL and self-heals on loss (RFC-002 §9). Short TTLs:
# typing decays in seconds; presence is refreshed by client heartbeats.
TYPING_TTL_SECONDS = 6
PRESENCE_TTL_SECONDS = 30
# "Viewing" presence powers collision detection (P1.7): who has a conversation open. Heartbeat-
# refreshed like online presence, so a slightly-longer TTL than typing.
VIEWING_TTL_SECONDS = 30
_TYPING_PREFIX = "rt:typing:"
_PRESENCE_PREFIX = "rt:presence:"
_VIEWING_PREFIX = "rt:view:"

# Reserved team tokens for inbox channels (a workspace-wide firehose + the unassigned bucket).
INBOX_ALL = "all"
INBOX_NONE = "none"


# --- Channels -----------------------------------------------------------------


def conv_channel(conversation_public_id: str) -> str:
    return f"conv:{conversation_public_id}"


def inbox_channel(workspace_public_id: str, team: str) -> str:
    return f"inbox:{workspace_public_id}:{team}"


def contact_channel(contact_public_id: str) -> str:
    """Per-contact widget channel (P1.8 in-app posts push to the contact's own feed)."""
    return f"contact:{contact_public_id}"


# --- Token minting ------------------------------------------------------------


def _exp(ttl_seconds: int | None) -> int:
    settings = get_settings()
    ttl = ttl_seconds if ttl_seconds is not None else settings.centrifugo_token_ttl_seconds
    return int((dt.datetime.now(dt.UTC) + dt.timedelta(seconds=ttl)).timestamp())


def mint_connection_token(
    sub: str,
    *,
    info: dict[str, Any] | None = None,
    channels: list[str] | None = None,
    ttl_seconds: int | None = None,
) -> str:
    """A Centrifugo connection JWT. ``channels`` pins the connection to a fixed allow-list
    (used for widgets); omit it for agents, who authorise channels via subscription tokens."""
    claims: dict[str, Any] = {"sub": sub, "exp": _exp(ttl_seconds)}
    if info:
        claims["info"] = info
    if channels:
        claims["channels"] = channels
    return jwt.encode(claims, get_settings().centrifugo_token_secret, algorithm=_ALG)


def mint_subscription_token(sub: str, channel: str, *, ttl_seconds: int | None = None) -> str:
    """A Centrifugo subscription JWT authorising exactly one channel for ``sub`` (must match the
    connection token's ``sub``)."""
    claims = {"sub": sub, "channel": channel, "exp": _exp(ttl_seconds)}
    return jwt.encode(claims, get_settings().centrifugo_token_secret, algorithm=_ALG)


def decode_centrifugo_token(token: str) -> dict[str, Any]:
    """Verify + decode a minted token (tests / introspection)."""
    decoded: dict[str, Any] = jwt.decode(
        token, get_settings().centrifugo_token_secret, algorithms=[_ALG]
    )
    return decoded


def agent_connection_token(*, admin_id: uuid.UUID, workspace_id: uuid.UUID, role: str) -> str:
    """Identity-only connection token for an agent. Channels are authorised per-subscription."""
    return mint_connection_token(
        str(admin_id),
        info={
            "kind": "agent",
            "ws": encode_public_id(IdPrefix.WORKSPACE, workspace_id),
            "role": role,
        },
    )


def widget_connection_token(
    *, workspace_id: uuid.UUID, contact_id: uuid.UUID, conversation_id: uuid.UUID
) -> str:
    """Connection token for a widget contact, pinned to exactly its own conversation channel.

    The ``channels`` claim is the gateway-side guarantee behind the P0.4 acceptance test: a
    widget token can only ever subscribe to ``conv:{its conversation}`` — Centrifugo rejects any
    other channel for the connection, and the API mints no subscription tokens for widgets.
    """
    conv_pub = encode_public_id(IdPrefix.CONVERSATION, conversation_id)
    contact_pub = encode_public_id(IdPrefix.CONTACT, contact_id)
    return mint_connection_token(
        f"contact:{contact_pub}",
        info={
            "kind": "contact",
            "ws": encode_public_id(IdPrefix.WORKSPACE, workspace_id),
            "conversation": conv_pub,
        },
        # Pinned allow-list: the contact's own conversation thread + its personal post feed (P1.8).
        channels=[conv_channel(conv_pub), contact_channel(contact_pub)],
    )


# --- Publish (server → Centrifugo HTTP API) -----------------------------------


async def publish(
    channel: str, data: dict[str, Any], *, client: httpx.AsyncClient | None = None
) -> None:
    """Publish one message to a Centrifugo channel. Raises on failure so callers that must not
    drop (the fanout consumer) can leave the outbox entry un-acked and retry. A shared ``client``
    is passed by the long-running fanout; request-path callers let it open an ephemeral one."""
    settings = get_settings()
    url = f"{settings.centrifugo_api_url.rstrip('/')}/api/publish"
    headers = {"X-API-Key": settings.centrifugo_api_key}
    body = {"channel": channel, "data": data}

    async def _do(c: httpx.AsyncClient) -> None:
        resp = await c.post(url, headers=headers, json=body)
        resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, dict) and payload.get("error"):
            raise RuntimeError(f"centrifugo publish error: {payload['error']}")

    if client is not None:
        await _do(client)
    else:
        async with httpx.AsyncClient(timeout=2.0) as ephemeral:
            await _do(ephemeral)


async def _relay_best_effort(channel: str, data: dict[str, Any]) -> None:
    """Publish for ephemeral signals (typing/presence): a gateway hiccup must never fail the
    request path, and the state already lives in Redis with a TTL."""
    try:
        await publish(channel, data)
    except Exception as exc:
        log.warning("realtime.relay_failed", channel=channel, error=str(exc))


# --- Typing (Redis TTL + relay) -----------------------------------------------


async def relay_typing(conversation_public_id: str, *, actor_kind: str, actor_id: str) -> None:
    redis = get_redis()
    await redis.set(
        f"{_TYPING_PREFIX}{conversation_public_id}:{actor_id}", actor_kind, ex=TYPING_TTL_SECONDS
    )
    await _relay_best_effort(
        conv_channel(conversation_public_id),
        {
            "topic": "typing",
            "conversation_id": conversation_public_id,
            "actor_kind": actor_kind,
            "actor_id": actor_id,
        },
    )


# --- Presence (Redis TTL + relay) ---------------------------------------------


async def mark_online(workspace_public_id: str, admin_public_id: str) -> None:
    redis = get_redis()
    await redis.set(
        f"{_PRESENCE_PREFIX}{workspace_public_id}:{admin_public_id}", "1", ex=PRESENCE_TTL_SECONDS
    )
    await _relay_best_effort(
        inbox_channel(workspace_public_id, INBOX_ALL),
        {
            "topic": "presence",
            "workspace_id": workspace_public_id,
            "admin_id": admin_public_id,
            "status": "online",
        },
    )


async def online_agents(workspace_public_id: str) -> list[str]:
    """Admin public ids currently online in a workspace (feeds P0.7's queue monitor)."""
    redis = get_redis()
    prefix = f"{_PRESENCE_PREFIX}{workspace_public_id}:"
    # ponytail: SCAN is O(online-agents) — fine at this scale; a per-workspace ZSET is the
    # upgrade if a workspace ever has thousands of simultaneously-online agents.
    return [key[len(prefix) :] async for key in redis.scan_iter(f"{prefix}*")]


# --- Viewing (collision detection — Redis TTL + relay, P1.7) ------------------


async def relay_viewing(conversation_public_id: str, *, admin_public_id: str) -> None:
    """Record that an agent has a conversation open (heartbeat) and relay a ``view`` event so other
    agents on the thread channel see the collision live. Redis-only with a TTL; never persisted."""
    redis = get_redis()
    await redis.set(
        f"{_VIEWING_PREFIX}{conversation_public_id}:{admin_public_id}",
        "1",
        ex=VIEWING_TTL_SECONDS,
    )
    await _relay_best_effort(
        conv_channel(conversation_public_id),
        {
            "topic": "view",
            "conversation_id": conversation_public_id,
            "admin_id": admin_public_id,
            "status": "viewing",
        },
    )


async def conversation_viewers(conversation_public_id: str) -> list[str]:
    """Admin public ids currently viewing a conversation (collision detection)."""
    redis = get_redis()
    prefix = f"{_VIEWING_PREFIX}{conversation_public_id}:"
    return [key[len(prefix) :] async for key in redis.scan_iter(f"{prefix}*")]


async def conversation_typers(conversation_public_id: str) -> list[dict[str, str]]:
    """Actors currently typing in a conversation as ``{actor_kind, actor_id}`` (agents/contacts)."""
    redis = get_redis()
    prefix = f"{_TYPING_PREFIX}{conversation_public_id}:"
    typers: list[dict[str, str]] = []
    async for key in redis.scan_iter(f"{prefix}*"):
        actor_kind = await redis.get(key)
        if actor_kind is not None:
            typers.append({"actor_id": key[len(prefix) :], "actor_kind": actor_kind})
    return typers
