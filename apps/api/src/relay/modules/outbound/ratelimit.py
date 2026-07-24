"""Send-rate token buckets + frequency caps for outbound (RFC-001 §6.7 / §9; P1.8).

Two independent guards, both Redis-backed (coordination only — a Redis blip degrades to the
provider circuit breaker as the backstop, never to a dropped send):

- **Token buckets** (global + per-tenant): smooth the campaign send rate to protect the provider.
  A single atomic Lua script does refill-and-take so concurrent workers can't overspend.
- **Frequency caps** (per-contact daily/weekly): a best-effort v0 guard read at send time and
  incremented only after a successful send (so a failed/retried attempt is not over-counted).

All caps are optional (``None`` in settings ⇒ uncapped, the dev/test default).
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from collections.abc import Awaitable
from typing import Any, cast

from relay.core.redis import get_redis
from relay.settings import get_settings

# Atomic token bucket: refill by elapsed*rate up to capacity, take `cost` if available.
# KEYS[1]=bucket; ARGV=rate, capacity, now_ms, cost. Returns 1 if taken, else 0.
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local cap = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then tokens = cap; ts = now end
local elapsed = (now - ts) / 1000.0
if elapsed < 0 then elapsed = 0 end
tokens = math.min(cap, tokens + elapsed * rate)
local allowed = 0
if tokens >= cost then tokens = tokens - cost; allowed = 1 end
redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', key, 60000)
return allowed
"""


def _global_key() -> str:
    return "outbound:tb:global"


def _tenant_key(workspace_id: uuid.UUID) -> str:
    return f"outbound:tb:ws:{workspace_id}"


async def acquire_send_tokens(workspace_id: uuid.UUID, *, cost: int = 1) -> bool:
    """Take one token from the global then per-tenant bucket. ``False`` if either is exhausted.

    Order (global first) fails fast on the scarcer shared resource. A token consumed on the global
    bucket when the tenant bucket then denies is an acceptable conservative over-count.
    """
    settings = get_settings()
    redis = get_redis()
    now_ms = int(time.time() * 1000)
    buckets = (
        (_global_key(), settings.outbound_global_send_rate_per_sec),
        (_tenant_key(workspace_id), settings.outbound_workspace_send_rate_per_sec),
    )
    for key, rate in buckets:
        if rate is None:
            continue
        capacity = max(rate, cost)  # allow a ~1s burst up to the rate
        # redis-py wants string ARGV; the Lua tonumber() coerces them back. The eval() return type
        # is a str|Awaitable union in the stubs — cast so ``await`` type-checks on the async client.
        allowed = await cast(
            "Awaitable[Any]",
            redis.eval(_TOKEN_BUCKET_LUA, 1, key, str(rate), str(capacity), str(now_ms), str(cost)),
        )
        if not int(allowed):
            return False
    return True


def _daily_key(workspace_id: uuid.UUID, contact_id: uuid.UUID) -> str:
    return f"outbound:freq:d:{workspace_id}:{contact_id}"


def _weekly_key(workspace_id: uuid.UUID, contact_id: uuid.UUID) -> str:
    return f"outbound:freq:w:{workspace_id}:{contact_id}"


def _seconds_to_end_of_day(now: dt.datetime) -> int:
    end = dt.datetime(now.year, now.month, now.day, tzinfo=dt.UTC) + dt.timedelta(days=1)
    return max(1, int((end - now).total_seconds()))


def _seconds_to_end_of_week(now: dt.datetime) -> int:
    start_of_day = dt.datetime(now.year, now.month, now.day, tzinfo=dt.UTC)
    end = start_of_day + dt.timedelta(days=7 - now.weekday())  # next Monday 00:00 UTC
    return max(1, int((end - now).total_seconds()))


async def frequency_exceeded(workspace_id: uuid.UUID, contact_id: uuid.UUID) -> bool:
    """Read-only frequency gate. ``True`` if a daily/weekly cap is already met for the contact."""
    settings = get_settings()
    daily_cap = settings.outbound_freq_cap_daily
    weekly_cap = settings.outbound_freq_cap_weekly
    if daily_cap is None and weekly_cap is None:
        return False
    redis = get_redis()
    if daily_cap is not None:
        daily = int(await redis.get(_daily_key(workspace_id, contact_id)) or 0)
        if daily >= daily_cap:
            return True
    if weekly_cap is not None:
        weekly = int(await redis.get(_weekly_key(workspace_id, contact_id)) or 0)
        if weekly >= weekly_cap:
            return True
    return False


async def record_frequency(workspace_id: uuid.UUID, contact_id: uuid.UUID) -> None:
    """Count one successful marketing send toward the contact's daily/weekly windows."""
    settings = get_settings()
    if settings.outbound_freq_cap_daily is None and settings.outbound_freq_cap_weekly is None:
        return
    redis = get_redis()
    now = dt.datetime.now(dt.UTC)
    if settings.outbound_freq_cap_daily is not None:
        key = _daily_key(workspace_id, contact_id)
        await redis.incr(key)
        await redis.expire(key, _seconds_to_end_of_day(now), nx=True)
    if settings.outbound_freq_cap_weekly is not None:
        key = _weekly_key(workspace_id, contact_id)
        await redis.incr(key)
        await redis.expire(key, _seconds_to_end_of_week(now), nx=True)
