"""Transactional outbox — the consistency spine (RFC-001 §6.5, RFC-002 §5.6).

Any state change with downstream effects (realtime fan-out, webhooks, workflow triggers,
AI turns, billing meters) writes an ``outbox`` row in the **same transaction** as the domain
write (master rule 2). A relay (``relay.core.outbox_relay``) publishes rows to Redis and
deletes them; consumers are idempotent, delivery is at-least-once, and per-aggregate ordering
is ``(aggregate_id, seq)``.

The outbox is *infrastructure, not a tenant table*: per RFC-002 §5.6 its columns are
``(id, aggregate, aggregate_id, seq, topic, payload, created_at, published_at)`` — deliberately
**no ``workspace_id`` and no RLS**, so the single relay can read every workspace's rows (an
RLS-forced ``app_rw`` with no ``app.ws`` set would see zero rows and could never drain). The
owning workspace travels inside ``payload`` for downstream routing (P0.4 fan-out, P0.11
webhooks). Only the relay ever reads it; request paths only ever append.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy import BigInteger, Text, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from relay.core.base_model import Base, TimestampMixin, UUIDPrimaryKey
from relay.core.ids import uuid7

# Postgres NOTIFY channel the relay LISTENs on (wakes it between polls — RFC-001 §6.5).
NOTIFY_CHANNEL = "relay_outbox"
# Redis stream the relay publishes to; consumers read it and dedupe by ``outbox_id``.
OUTBOX_STREAM = "relay:outbox"


class OutboxMessage(UUIDPrimaryKey, TimestampMixin, Base):
    """One durable integration event. Not tenant-scoped (see module docstring)."""

    __tablename__ = "outbox"
    __table_args__ = (
        # Guards per-aggregate ordering: a duplicate seq (which would only happen if a caller
        # emitted without holding the aggregate's lock — see ``emit``) fails loudly instead of
        # silently mis-ordering the stream.
        sa.UniqueConstraint("aggregate_id", "seq", name="uq_outbox_aggregate_id_seq"),
    )

    aggregate: Mapped[str] = mapped_column(Text, nullable=False)  # e.g. "conversation"
    aggregate_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    topic: Mapped[str] = mapped_column(Text, nullable=False)  # e.g. "conversation.part.created"
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    published_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


async def emit(
    session: AsyncSession,
    *,
    aggregate: str,
    aggregate_id: uuid.UUID,
    topic: str,
    payload: dict[str, Any],
) -> None:
    """Append an outbox row **in the caller's transaction** (master rule 2).

    ``seq`` is the next value for this ``aggregate_id`` (``MAX(seq)+1``). That is race-free as
    long as the caller holds a row lock on the aggregate for the txn's duration — W1 does,
    because it UPDATEs the conversation head *before* emitting, so concurrent writes to the same
    conversation serialise on that row lock and each sees the prior seq. The
    ``UNIQUE(aggregate_id, seq)`` constraint is the backstop if that assumption is ever broken.
    """
    next_seq = (
        select(func.coalesce(func.max(OutboxMessage.seq), 0) + 1)
        .where(OutboxMessage.aggregate_id == aggregate_id)
        .scalar_subquery()
    )
    await session.execute(
        sa.insert(OutboxMessage).values(
            id=uuid7(),
            aggregate=aggregate,
            aggregate_id=aggregate_id,
            seq=next_seq,
            topic=topic,
            payload=payload,
        )
    )
    # Wake the relay immediately; the notification fires on commit (poll is the fallback).
    await session.execute(sa.text(f"NOTIFY {NOTIFY_CHANNEL}"))
