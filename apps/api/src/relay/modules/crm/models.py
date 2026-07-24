"""SQLAlchemy models for the ``crm`` module (RFC-002 §5.4).

Tables:
- ``contacts``      — users/leads; ``citext`` email; partial unique indexes; trigram name
                      typeahead; ``custom`` JSONB typed by ``attribute_definitions``.
- ``companies``     — tenant company records; optional ``external_id``; ``custom`` JSONB.
- ``contact_companies`` — many-to-many contact↔company link.
- ``attribute_definitions`` — the *only* schema for ``custom`` JSONB (the swamp guard,
                      RFC-002 §12); typed string/number/boolean/date/list, scoped per entity.
- ``events``        — append-only firehose (RFC-002 §5.4): ``bigint`` identity PK,
                      monthly RANGE partitions, BRIN on ``created_at``. Not exposed via a
                      public id; no FK to contacts (append speed, mirrors §10 audit_logs).

Indexes that need Postgres-specific features (partial WHERE, GIN opclasses, partition
templates) are created in the migration (0002_crm); that file is the authoritative DDL.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Identity, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from relay.core.base_model import Base, TimestampMixin, UUIDPrimaryKey, WorkspaceScoped

# --- Closed-ish sets: text + CHECK (RFC-002 §5.1) -----------------------------

CONTACT_KINDS: tuple[str, ...] = ("user", "lead")
_KIND_CHECK = "kind IN ('user', 'lead')"

ATTRIBUTE_TYPES: tuple[str, ...] = ("string", "number", "boolean", "date", "list")
_ATTR_TYPE_CHECK = "data_type IN ('string', 'number', 'boolean', 'date', 'list')"

ATTRIBUTE_ENTITIES: tuple[str, ...] = ("contact", "company")
_ATTR_ENTITY_CHECK = "entity IN ('contact', 'company')"


class Contact(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """A person known to a workspace (``user`` once identified, else ``lead``).

    Identity keys (per workspace, soft-delete-aware, enforced by partial unique indexes in
    the migration): ``external_id`` (the tenant's own user id) and, for ``kind='user'``,
    ``email``. ``custom`` is validated against ``attribute_definitions`` at write time.
    """

    __tablename__ = "contacts"
    __table_args__ = (CheckConstraint(_KIND_CHECK, name="kind_valid"),)

    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'user'"))
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str | None] = mapped_column(CITEXT, nullable=True)
    phone: Mapped[str | None] = mapped_column(Text, nullable=True)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    custom: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    deleted_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class Company(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """A company/organization record. ``external_id`` is the tenant's own company id."""

    __tablename__ = "companies"

    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    domain: Mapped[str | None] = mapped_column(Text, nullable=True)
    custom: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )


class ContactCompany(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """Contact ↔ company membership (many-to-many)."""

    __tablename__ = "contact_companies"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "contact_id",
            "company_id",
            name="uq_contact_companies_contact_company",
        ),
    )

    contact_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )


class AttributeDefinition(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """Typed schema for a ``custom`` JSONB key on contacts/companies (RFC-002 §12).

    The write-time validator (and a later nightly type-audit) read these. ``data_type`` is
    one of string/number/boolean/date/list; ``entity`` scopes it to contacts or companies.
    """

    __tablename__ = "attribute_definitions"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "entity", "name", name="uq_attribute_definitions_entity_name"
        ),
        CheckConstraint(_ATTR_TYPE_CHECK, name="data_type_valid"),
        CheckConstraint(_ATTR_ENTITY_CHECK, name="entity_valid"),
    )

    entity: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'contact'"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    data_type: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)


class Event(Base):
    """Append-only analytics event (RFC-002 §5.4 — the firehose, W3).

    ``bigint`` identity PK (never exposed), BRIN on ``created_at``. No FK to contacts (append
    speed). Loaded by the analytics drain via temp-stage COPY + INSERT…SELECT (COPY FROM is
    unsupported on RLS tables — the drain keeps the RLS backstop by inserting under ``app.ws``).
    RLS is enabled + forced by ``create_tenant_table`` in the migration. (Was monthly-partitioned;
    de-partitioned in 0018.)
    """

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    contact_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    properties: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    created_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
