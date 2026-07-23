"""Public-API cross-cutting concerns for API-key traffic (P0.11, RFC-001 §10).

The ``/v0`` surface is shared: the first-party agent app authenticates with JWTs, third parties
with API keys. This middleware runs *inner* of ``TenancyMiddleware`` (so the principal is already
resolved) and is a **no-op for anything that is not an ``api_key`` principal** — JWT/agent traffic
is never rate-limited and can still reach admin-only routes. For API-key principals it enforces,
in order:

1. an explicit **route allowlist** (default-deny) — keys reach only the P0.11 public resources
   (contacts, conversations, articles-read, events), never ``/v0/api-keys``, members, teams,
   auth, settings, realtime, or the knowledge admin surface;
2. **scope** (``read`` for safe methods, ``write`` otherwise; ``write`` implies ``read``);
3. a **per-workspace token-bucket rate limit** (Redis), stamping ``X-RateLimit-*`` on every
   response and ``Retry-After`` on a 429.

Denials are *returned* as ``ORJSONResponse`` (not raised): a user middleware runs outside FastAPI's
exception-handling layer, so a raised ``AppError`` here would become a 500. The envelope is built
with the same ``core.errors`` helper the handlers use, so the shape is identical.

This module lives in the kernel and imports no feature module (the ``last_used_at`` touch uses raw
SQL, not the ``ApiKey`` model) — the kernel must never depend on a module.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from relay.core.db import session_scope
from relay.core.errors import _envelope
from relay.core.logging import get_logger
from relay.core.redis import get_redis
from relay.settings import Settings, get_settings

log = get_logger(__name__)

# Routes an API key may call, matched against ``request.url.path`` (default-deny). Exactly the
# P0.11 resource set: contacts (CRUD/identify), conversations (list/get/create/reply),
# articles (read), events (track). ``identify`` is only POST, so it never collides with the
# ``contacts/{id}`` (GET/PATCH/DELETE) pattern.
_PUBLIC_API_ROUTES: list[tuple[frozenset[str], re.Pattern[str]]] = [
    (frozenset({"GET", "POST"}), re.compile(r"^/v0/contacts$")),
    (frozenset({"POST"}), re.compile(r"^/v0/contacts/identify$")),
    (frozenset({"GET", "PATCH", "DELETE"}), re.compile(r"^/v0/contacts/[^/]+$")),
    (frozenset({"GET"}), re.compile(r"^/v0/contacts/[^/]+/events$")),
    (frozenset({"POST"}), re.compile(r"^/v0/events/track$")),
    (frozenset({"GET", "POST"}), re.compile(r"^/v0/conversations$")),
    (frozenset({"GET"}), re.compile(r"^/v0/conversations/[^/]+$")),
    (frozenset({"POST"}), re.compile(r"^/v0/conversations/[^/]+/reply$")),
    (frozenset({"GET"}), re.compile(r"^/v0/articles$")),
    (frozenset({"GET"}), re.compile(r"^/v0/articles/[^/]+$")),
]

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Atomic token-bucket refill+consume. ``now`` is taken from Redis ``TIME`` so the decision is
# immune to cross-node clock skew. Guards ``rate == 0`` (tests force a 429 with no refill).
_TOKEN_BUCKET_LUA = """
local cap = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then tokens = cap; ts = now end
local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(cap, tokens + (elapsed / 1000.0) * rate)
local allowed = 0
if tokens >= cost then allowed = 1; tokens = tokens - cost end
redis.call('HMSET', KEYS[1], 'tokens', tokens, 'ts', now)
local ttl = 3600
if rate > 0 then ttl = math.ceil(cap / rate) + 1 end
redis.call('EXPIRE', KEYS[1], ttl)
local retry_ms = 0
if allowed == 0 then
  if rate > 0 then retry_ms = math.ceil(((cost - tokens) / rate) * 1000) else retry_ms = 3600000 end
end
local reset_ms
if rate > 0 then reset_ms = math.ceil(((cap - tokens) / rate) * 1000) else reset_ms = 3600000 end
return {allowed, math.floor(tokens), retry_ms, reset_ms, now}
"""


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_epoch: int
    retry_after: int


def _route_allowed(method: str, path: str) -> bool:
    return any(method in methods and pat.match(path) for methods, pat in _PUBLIC_API_ROUTES)


def _required_scope(method: str) -> str:
    return "read" if method in _SAFE_METHODS else "write"


def _has_scope(scopes: tuple[str, ...], needed: str) -> bool:
    if needed == "read":
        return "read" in scopes or "write" in scopes  # write implies read
    return needed in scopes


def _deny(
    status_code: int, code: str, message: str, details: dict[str, Any] | None = None
) -> Response:
    from fastapi.responses import ORJSONResponse

    return ORJSONResponse(status_code=status_code, content=_envelope(code, message, details))


async def _check_rate_limit(workspace_id: Any, settings: Settings) -> RateLimitResult | None:
    """Consume one token for ``workspace_id``. Returns None (fail-open) if Redis is unavailable."""
    client = get_redis()
    try:
        raw = await cast(
            "Awaitable[list[Any]]",
            client.eval(
                _TOKEN_BUCKET_LUA,
                1,
                f"ratelimit:{workspace_id}",
                str(settings.public_api_rate_capacity),
                str(settings.public_api_rate_refill_per_sec),
                "1",
            ),
        )
    except Exception as exc:  # Redis down → don't wedge the API (availability > strict quota)
        log.warning("public_api.ratelimit.unavailable", error=str(exc))
        return None
    allowed, remaining, retry_ms, reset_ms, now_ms = (int(v) for v in raw)
    return RateLimitResult(
        allowed=bool(allowed),
        limit=settings.public_api_rate_capacity,
        remaining=max(0, remaining),
        reset_epoch=(now_ms + reset_ms) // 1000,
        retry_after=max(1, (retry_ms + 999) // 1000),
    )


def _stamp_rate_headers(response: Response, rl: RateLimitResult) -> None:
    response.headers["X-RateLimit-Limit"] = str(rl.limit)
    response.headers["X-RateLimit-Remaining"] = str(rl.remaining)
    response.headers["X-RateLimit-Reset"] = str(rl.reset_epoch)


async def _touch_last_used(request: Request, workspace_id: Any) -> None:
    """Best-effort ``api_keys.last_used_at`` bump, throttled to ≤1 write/60s/key so it never
    contends the row or slows the response. Raw SQL keeps the kernel free of a module import."""
    key_id = getattr(request.state, "api_key_id", None)
    if key_id is None:
        return
    redis = get_redis()
    try:
        first = await redis.set(f"apikey:lastused:{key_id}", "1", nx=True, ex=60)
        if not first:
            return
        async with session_scope(workspace_id) as session:
            await session.execute(
                text("UPDATE api_keys SET last_used_at = now() WHERE id = :id"), {"id": str(key_id)}
            )
    except Exception as exc:  # purely best-effort telemetry; never fail the request
        log.warning("public_api.last_used.failed", error=str(exc))


class PublicApiMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        principal = getattr(request.state, "principal", None)
        if principal is None or principal.kind != "api_key":
            return await call_next(request)

        method, path = request.method, request.url.path
        if not _route_allowed(method, path):
            return _deny(403, "permission_denied", "route not available to API keys")
        needed = _required_scope(method)
        if not _has_scope(principal.scopes, needed):
            return _deny(
                403,
                "permission_denied",
                f"api key missing required scope '{needed}'",
                {"required_scope": needed},
            )

        settings = get_settings()
        rl: RateLimitResult | None = None
        if settings.public_api_rate_limit_enabled:
            rl = await _check_rate_limit(principal.workspace_id, settings)
            if rl is not None and not rl.allowed:
                resp = _deny(429, "rate_limited", "public API rate limit exceeded")
                _stamp_rate_headers(resp, rl)
                resp.headers["Retry-After"] = str(rl.retry_after)
                return resp

        response = await call_next(request)
        if rl is not None:
            _stamp_rate_headers(response, rl)
        await _touch_last_used(request, principal.workspace_id)
        return response
