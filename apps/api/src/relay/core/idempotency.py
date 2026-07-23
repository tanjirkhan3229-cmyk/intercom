"""Idempotency keys for mutating endpoints (RFC-002 §5.6, §7 — W1/W4 client retries).

A mutating endpoint decorated with :func:`idempotent` honours an ``Idempotency-Key`` header:
the first request with a given key runs normally and its response is stored; any later request
with the same key replays that stored response without re-running the handler — so a retried
"send" returns the original part and creates exactly one row.

**Correctness under concurrency.** The claim row is inserted (``INSERT … ON CONFLICT DO
NOTHING``) *before* the handler runs, inside the request's transaction. Two concurrent requests
with the same key therefore contend on the ``(workspace_id, key)`` unique index: the second
INSERT blocks on the first's uncommitted row until the first commits, then returns zero rows and
replays the now-committed response. If the first request errors, its whole transaction (claim
included) rolls back, so the key is free to be claimed again. This makes the domain write, the
outbox row, and the idempotency record one atomic unit.

``idempotency_keys`` *is* a tenant table (it carries ``workspace_id`` — RLS enabled + forced);
every access here runs under the request's ``app.ws``.
"""

from __future__ import annotations

import datetime as dt
import functools
import hashlib
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import sqlalchemy as sa
from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import ORJSONResponse
from sqlalchemy import Integer, Text, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from relay.core.base_model import Base, TimestampMixin, UUIDPrimaryKey, WorkspaceScoped
from relay.core.errors import ConflictError
from relay.core.ids import uuid7
from relay.core.principal import Principal

IDEMPOTENCY_HEADER = "Idempotency-Key"
# How long a stored response can be replayed. Purged by a housekeeping task (see migration).
IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60


class IdempotencyKey(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """A claimed idempotency key and (once the handler finishes) its stored response."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "key", name="uq_idempotency_keys_workspace_id_key"),
    )

    key: Mapped[str] = mapped_column(Text, nullable=False)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    expires_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def _request_hash(request: Request) -> str:
    """Fingerprint method + path + body so the same key on a *different* request is rejected."""
    body = await request.body()  # cached by Starlette; FastAPI already read it to parse the body
    h = hashlib.sha256()
    h.update(request.method.encode())
    h.update(b"\x00")
    h.update(request.url.path.encode())
    h.update(b"\x00")
    h.update(body)
    return h.hexdigest()


async def _claim_or_replay(
    session: AsyncSession, principal: Principal, request: Request, key: str, status_code: int
) -> ORJSONResponse | None:
    """Return ``None`` if we claimed the key (caller should run the handler), else a replay
    response for the stored original."""
    request_hash = await _request_hash(request)
    claim = (
        pg_insert(IdempotencyKey)
        .values(
            id=uuid7(),
            workspace_id=principal.workspace_id,
            key=key,
            request_hash=request_hash,
            expires_at=_now() + dt.timedelta(seconds=IDEMPOTENCY_TTL_SECONDS),
        )
        .on_conflict_do_nothing(index_elements=[IdempotencyKey.workspace_id, IdempotencyKey.key])
        .returning(IdempotencyKey.id)
    )
    claimed = (await session.execute(claim)).scalar_one_or_none()
    if claimed is not None:
        return None  # we own this key — proceed to the handler

    # Conflict: a committed row already exists for this key (RLS scopes the lookup to us).
    existing = (
        await session.execute(select(IdempotencyKey).where(IdempotencyKey.key == key))
    ).scalar_one()
    if existing.request_hash != request_hash:
        raise ConflictError("Idempotency-Key was already used with a different request")
    if existing.response is None:
        # Original still in flight (it errored after claiming, or a rare interleaving). Retry.
        raise ConflictError("a request with this Idempotency-Key is still in progress")
    return ORJSONResponse(
        status_code=existing.status_code or status_code, content=existing.response
    )


async def _store(session: AsyncSession, key: str, result: Any, status_code: int) -> None:
    await session.execute(
        update(IdempotencyKey)
        .where(IdempotencyKey.key == key)  # RLS scopes to this workspace's single row
        .values(response=jsonable_encoder(result), status_code=status_code)
    )


F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def idempotent(*, status_code: int = 200) -> Callable[[F], F]:
    """Decorate a mutating endpoint so it honours the ``Idempotency-Key`` header.

    The endpoint must declare ``request: Request``, ``session`` (the request session) and
    ``principal`` parameters (the messaging routes do). ``status_code`` is the route's success
    status, replayed verbatim on a duplicate. With no header the handler runs unchanged.
    """

    def deco(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            request: Request | None = kwargs.get("request")
            session: AsyncSession | None = kwargs.get("session")
            principal: Principal | None = kwargs.get("principal")
            key = request.headers.get(IDEMPOTENCY_HEADER) if request is not None else None
            if not key or session is None or principal is None:
                return await func(*args, **kwargs)
            replay = await _claim_or_replay(session, principal, request, key, status_code)  # type: ignore[arg-type]
            if replay is not None:
                return replay
            result = await func(*args, **kwargs)
            await _store(session, key, result, status_code)
            return result

        return wrapper  # type: ignore[return-value]

    return deco
