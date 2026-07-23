"""SQLAlchemy models for the ``reporting`` module (P0.9 — RFC-000 §2.9, RFC-002 §5.6, §2 R4/R9).

Reporting is a **read-optimised projection** of the messaging domain, maintained entirely off the
transactional outbox (RFC-001 §6.5) — it never scans the hot ``conversation_parts`` firehose
(P0.9 acceptance: "no reporting query touches ``conversation_parts`` raw"). Two tenant tables:

- ``conversation_metrics`` — one upserted row per conversation, folded from the conversation's
  outbox events by the ``reporting-metrics`` consumer (``consumer.py`` + the pure ``reducer.py``).
  Carries the responsiveness/resolution/CSAT facts (first_response_s, resolution_s, replies_count,
  rating) plus the denormalised ``team_id``/``assignee_id`` for filtering. ``last_seq`` is the max
  per-aggregate outbox ``seq`` applied, so redelivered/replayed events are idempotent.
- ``daily_rollups`` — per ``(workspace_id, day, team_id)`` aggregate, recomputed idempotently by the
  ``analytics`` rollup task from ``conversation_metrics`` (never from parts). Composable across days
  by summation (counts, sums, and a per-star ``rating_histogram``), which is what the volume + CSAT
  endpoints read. Responsiveness percentiles don't compose across day-rows, so that endpoint reads
  ``conversation_metrics`` directly (still off the parts table).

RLS is enabled + FORCED on both by ``create_tenant_table`` in ``0004_reporting`` (the authoritative
DDL, incl. the ``relay_reporting_rollup`` SECURITY DEFINER function). Never import this module from
another module — cross-module access is via ``service`` / ``events``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy import BigInteger, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from relay.core.base_model import Base, TimestampMixin, UUIDPrimaryKey, WorkspaceScoped


class ConversationMetric(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """Per-conversation metrics, folded from outbox events (idempotent via ``last_seq``)."""

    __tablename__ = "conversation_metrics"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "conversation_id",
            name="uq_conversation_metrics_workspace_id_conversation_id",
        ),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    # Denormalised from events for filtering/grouping (no FK — reporting stays decoupled).
    team_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)

    opened_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    first_admin_reply_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    first_response_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    closed_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    resolution_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reopen_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("0"))
    replies_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("0"))
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rated_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    # Max per-aggregate outbox ``seq`` folded into this row — the idempotent-replay watermark.
    last_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=sa.text("0"))


class DailyRollup(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """Per (workspace, day, team) aggregate. Recomputed idempotently; ``created_at`` is preserved on
    conflict so a re-run produces byte-identical rows (P0.9 acceptance)."""

    __tablename__ = "daily_rollups"
    __table_args__ = (
        # NULLS NOT DISTINCT (PG15+) so the "no team" bucket (team_id IS NULL) is a single upsert
        # target — ON CONFLICT (workspace_id, day, team_id) then infers this arbiter for NULL teams.
        UniqueConstraint(
            "workspace_id",
            "day",
            "team_id",
            name="uq_daily_rollups_workspace_id_day_team_id",
            postgresql_nulls_not_distinct=True,
        ),
    )

    day: Mapped[dt.date] = mapped_column(sa.Date(), nullable=False)
    team_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)

    conversations_opened: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa.text("0")
    )
    conversations_closed: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa.text("0")
    )
    replies_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("0"))
    first_response_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa.text("0")
    )
    first_response_sum_s: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=sa.text("0")
    )
    rating_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("0"))
    rating_sum: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=sa.text("0"))
    rating_histogram: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
