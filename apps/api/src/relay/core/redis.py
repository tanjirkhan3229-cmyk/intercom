"""Redis accessors (RFC-002 §2, §9 — cache/coordination + ephemera only, never truth).

Two client flavours share one URL (``settings.redis_cache_url``):
- ``get_redis()``      — ``redis.asyncio`` client for ``async`` request paths (event buffering,
                         presence/typing TTL keys, unread counts).
- ``get_redis_sync()`` — a synchronous client for Celery tasks, which run in a sync context.

Both are module-level singletons; the async client is rebound per event loop by tests.
"""

from __future__ import annotations

import redis
import redis.asyncio as aredis

from relay.settings import get_settings

_async_client: aredis.Redis | None = None
_sync_client: redis.Redis | None = None


def get_redis() -> aredis.Redis:
    """Async Redis client for request paths. Decodes responses to ``str``."""
    global _async_client
    if _async_client is None:
        _async_client = aredis.from_url(get_settings().redis_cache_url, decode_responses=True)
    return _async_client


def get_redis_sync() -> redis.Redis:
    """Synchronous Redis client for Celery tasks. Decodes responses to ``str``."""
    global _sync_client
    if _sync_client is None:
        _sync_client = redis.from_url(get_settings().redis_cache_url, decode_responses=True)
    return _sync_client


async def reset_async_redis() -> None:
    """Dispose the async client (used by tests to rebind to the current event loop)."""
    global _async_client
    if _async_client is not None:
        await _async_client.aclose()
        _async_client = None
