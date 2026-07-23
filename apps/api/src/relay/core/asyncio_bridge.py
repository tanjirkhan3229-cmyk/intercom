"""Run coroutines from sync Celery tasks on ONE persistent per-process event loop.

The async service layer (messaging W1, crm upserts) is reused verbatim from sync Celery tasks
via this bridge, so channel adapters never re-implement the outbox-seq / row-lock invariants.

Why not ``asyncio.run`` per task: the module-global asyncpg engine (``core/db.py``) binds its
connection pool to whichever event loop first opened a connection. ``asyncio.run`` creates and
**closes** a fresh loop each call, so the second task in a worker process
(``worker_prefetch_multiplier=1`` runs tasks sequentially in one process) would reuse the global
engine against a closed loop and raise ``got Future attached to a different loop``. We instead
create one loop lazily, cache it for the process, and drive every coroutine on it — the engine is
created on that loop and lives as long as the worker.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

_loop: asyncio.AbstractEventLoop | None = None


def get_loop() -> asyncio.AbstractEventLoop:
    """Return this process's persistent event loop, creating it on first use."""
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop


def run_coro[T](coro: Coroutine[Any, Any, T]) -> T:
    """Drive ``coro`` to completion on the persistent per-process loop (sync callers only)."""
    return get_loop().run_until_complete(coro)
