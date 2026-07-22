"""Database engines, sessions, and the tenancy GUC (RFC-002 §7, §9).

The single most important rule in the codebase: **every request that touches a tenant
table runs inside a transaction that has set ``app.ws``** to the caller's workspace id.
RLS policies (``ws_isolation``) read that GUC; if it is unset, tenant tables return zero
rows. The session middleware (identity module) is the one place that sets it.

asyncpg + transaction-pooling trap (RFC-002 §9): client prepared-statement caching is
disabled so the app is safe behind PgBouncer/RDS Proxy in transaction mode.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from relay.settings import get_settings

# Postgres GUC name that RLS policies read. Kept in one place; referenced by policies.
WORKSPACE_GUC = "app.ws"

_engine: AsyncEngine | None = None
_engine_ro: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _make_engine(dsn: str) -> AsyncEngine:
    return create_async_engine(
        dsn,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        # RFC-002 §9: disable asyncpg's prepared-statement cache for pooler safety.
        connect_args={"statement_cache_size": 0},
    )


def get_engine() -> AsyncEngine:
    """Writer engine (app_rw). Used for R1/R2 read-your-writes and all writes."""
    global _engine
    if _engine is None:
        _engine = _make_engine(get_settings().database_url)
    return _engine


def get_ro_engine() -> AsyncEngine:
    """Read-only engine (app_ro / replicas). Used for replica-tolerant reads (R3/R5/...)."""
    global _engine_ro
    if _engine_ro is None:
        _engine_ro = _make_engine(get_settings().database_url_ro)
    return _engine_ro


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


async def set_workspace_guc(session: AsyncSession, workspace_id: uuid.UUID) -> None:
    """Set the tenant GUC for the current transaction (``SET LOCAL`` semantics).

    Uses ``set_config(name, value, is_local=true)`` because it accepts a bind parameter,
    unlike the ``SET LOCAL`` statement. Scoped to the transaction; reset on commit/rollback.
    """
    from sqlalchemy import text

    await session.execute(
        text("SELECT set_config(:name, :val, true)"),
        {"name": WORKSPACE_GUC, "val": str(workspace_id)},
    )


@asynccontextmanager
async def session_scope(workspace_id: uuid.UUID | None = None) -> AsyncIterator[AsyncSession]:
    """Open a transactional session, optionally pinned to a workspace via the RLS GUC.

    Used by workers/CLI and tests. Request handlers get their session from middleware
    (which additionally derives the workspace from the authenticated principal).
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        if workspace_id is not None:
            await set_workspace_guc(session, workspace_id)
        yield session


async def db_healthcheck() -> bool:
    from sqlalchemy import text

    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
