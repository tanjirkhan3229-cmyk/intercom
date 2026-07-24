"""SQLAlchemy models for the ``automation`` module (P1.5, RFC-002 §5.6).

The no-code workflow engine's durable state. All are **regular tenant tables** (RLS enabled +
forced by ``create_tenant_table`` in migration 0009) — none partitioned (RFC-002 §5.6 lists the
workflow tables without partitioning; volumes are far below the parts/events firehoses).

- ``workflows``          — the logical automation; points at its currently-active version.
- ``workflow_versions``  — an immutable graph snapshot; **runs pin a version** so editing/publishing
                           never mutates an in-flight run (RFC-001 §6.7).
- ``workflow_runs``      — one execution instance. UNIQUE ``(workspace_id, workflow_id,
                           dedupe_key)`` makes run creation exactly-once under at-least-once
                           delivery.
- ``workflow_run_steps`` — **the exactly-once-effects ledger**: UNIQUE ``(run_id, node_id)``
                           (RFC-002 §5.6 "unique (run_id, step_id)"). A replayed advance sees the
                           committed step row and skips the effect.
- ``timers``             — durable waits (W6). Claimed by beat via ``FOR UPDATE SKIP LOCKED`` (the
                           claim fn + partial index are authored in the migration).

Postgres-specific DDL (partial index, the SECURITY DEFINER claim function) lives in the migration,
which is the authoritative schema. Never import this module from another — go through ``service``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from relay.core.base_model import Base, TimestampMixin, UUIDPrimaryKey, WorkspaceScoped

# --- Closed-ish sets (text + CHECK, validated in the service/executor) --------

WORKFLOW_STATUSES: tuple[str, ...] = ("inactive", "active")
VERSION_STATUSES: tuple[str, ...] = ("draft", "published", "archived")
RUN_STATUSES: tuple[str, ...] = (
    "running",  # actively advancing (or enqueued to)
    "waiting",  # parked on a durable timer (a `wait` node)
    "suspended",  # parked on an external action (`call_webhook`) in flight
    "awaiting_input",  # parked on a `bot_step`, waiting for the contact's reply
    "completed",  # reached an `end` node
    "failed",  # hit an unrecoverable error
    "cancelled",  # cancelled by an admin
)
STEP_STATUSES: tuple[str, ...] = ("started", "done", "failed", "skipped")
TIMER_STATUSES: tuple[str, ...] = ("pending", "fired", "cancelled")

_WORKFLOW_STATUS_CHECK = "status IN ('inactive', 'active')"
_VERSION_STATUS_CHECK = "status IN ('draft', 'published', 'archived')"
_RUN_STATUS_CHECK = (
    "status IN ('running', 'waiting', 'suspended', 'awaiting_input', "
    "'completed', 'failed', 'cancelled')"
)
_STEP_STATUS_CHECK = "status IN ('started', 'done', 'failed', 'skipped')"
_TIMER_STATUS_CHECK = "status IN ('pending', 'fired', 'cancelled')"


class Workflow(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """A logical no-code automation. ``active_version_id`` is the version new runs are created from
    while ``status='active'``; publishing swaps it without touching in-flight runs."""

    __tablename__ = "workflows"
    __table_args__ = (CheckConstraint(_WORKFLOW_STATUS_CHECK, name="status_valid"),)

    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'inactive'"))
    # No FK: avoids a workflows↔workflow_versions circular FK. Integrity is enforced in the service
    # (publish only sets it to a version of this workflow).
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class WorkflowVersion(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """An immutable graph snapshot. ``trigger_key`` is denormalised from the graph's trigger node so
    the trigger consumer filters active versions by trigger without parsing the graph."""

    __tablename__ = "workflow_versions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "workflow_id", "version", name="uq_version_number"),
        CheckConstraint(_VERSION_STATUS_CHECK, name="status_valid"),
    )

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    graph: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    trigger_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'draft'"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )


class WorkflowRun(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """One execution of a pinned workflow version.

    ``dedupe_key`` makes creation exactly-once: the trigger consumer derives it from the source
    event (``<trigger_key>:<outbox_id>``) and inserts ON CONFLICT DO NOTHING, so an at-least-once
    redelivery of the same event yields a single run. ``context`` is the run's working memory (the
    trigger payload + collected bot answers + action results), read by condition predicates.
    """

    __tablename__ = "workflow_runs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "workflow_id", "dedupe_key", name="uq_run_dedupe"),
        CheckConstraint(_RUN_STATUS_CHECK, name="status_valid"),
    )

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    # No FK to workflow_versions: a version is never deleted while runs reference it, and avoiding
    # the FK keeps the hot run-advance path lock-light. Integrity is a service invariant.
    workflow_version_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'running'"))
    trigger_topic: Mapped[str] = mapped_column(Text, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False)
    subject_kind: Mapped[str | None] = mapped_column(Text, nullable=True)  # conversation | contact
    subject_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    context: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    current_node_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class WorkflowRunStep(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """The exactly-once-effects ledger (RFC-002 §5.6). One row per (run, graph node). The executor
    inserts it ON CONFLICT DO NOTHING *before* performing a node's effect, in the same transaction,
    so a replayed advance sees the committed row and never repeats the effect."""

    __tablename__ = "workflow_run_steps"
    __table_args__ = (
        UniqueConstraint("run_id", "node_id", name="uq_run_step"),
        CheckConstraint(_STEP_STATUS_CHECK, name="status_valid"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'started'"))
    action_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("0"))
    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class Timer(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """A durable wait (W6). ``beat`` claims due rows via ``FOR UPDATE SKIP LOCKED`` (the
    ``relay_claim_due_timers`` function in the migration) and enqueues ``fire_timer``;
    ``claimed_by`` + ``claimed_at`` are a lease so a crashed claim is reclaimed after it lapses."""

    __tablename__ = "timers"
    __table_args__ = (CheckConstraint(_TIMER_STATUS_CHECK, name="status_valid"),)

    run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    fire_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'pending'"))
    claimed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
