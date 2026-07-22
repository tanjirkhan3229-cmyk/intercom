"""SQLAlchemy declarative base + shared mixins (RFC-002 §5.1).

- Deterministic constraint/index names (Alembic diffs stay stable).
- UUIDv7 primary keys, app-generated.
- ``WorkspaceScoped`` mixin adds ``workspace_id`` for tenant tables; RLS enable/force is
  applied separately by the ``create_tenant_table()`` migration helper (RFC-002 §7).
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, MetaData, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from relay.core.ids import uuid7

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPrimaryKey:
    """App-generated UUIDv7 primary key (time-ordered; safe to expose via base62)."""

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid7
    )


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WorkspaceScoped:
    """Adds the tenant discriminator. Every tenant table carries ``workspace_id`` and is
    protected by RLS (applied by create_tenant_table). Composite indexes must lead with it.
    """

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
