"""SQLAlchemy models for the ``knowledge`` module (RFC-000 §2.5, RFC-002 §5.5).

P0.8 scope — the Help Center content model (chunks/embeddings for retrieval land in P1.1):

- ``help_centers``        — one row per workspace: theming (logo, colors), default locale,
                            and the ``custom_domain`` schema field (custom domains ship in
                            phase 2 — the column exists now, unused).
- ``collections``         — article groupings; ``slug`` unique per workspace; optional
                            self-referential ``parent_id`` for nesting; ``position`` orders them.
- ``articles``            — block-based ``body`` (JSONB) + a service-computed ``body_text``
                            plaintext that feeds the **generated** ``search_tsv`` (title
                            weighted ``A`` above body ``B`` so title matches rank first — the
                            P0.8 FTS acceptance). ``status`` draft|published; per-article SEO
                            fields; soft-deleted via ``deleted_at``.
- ``article_translations`` — schema only this phase (UI later, RFC-000 §2.5): per-locale
                            title/body with the same weighted ``search_tsv``.

Postgres-specific bits (partial unique indexes, GIN on the tsvector, the generated column,
the ``custom_domain`` partial-unique) are authored in the migration (0004_knowledge), which
is the authoritative DDL. Never import this module from another module — go through ``service``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import CITEXT, JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from relay.core.base_model import Base, TimestampMixin, UUIDPrimaryKey, WorkspaceScoped

# --- Closed sets: text + CHECK (RFC-002 §5.1) ---------------------------------

ARTICLE_STATUSES: tuple[str, ...] = ("draft", "published")
_STATUS_CHECK = "status IN ('draft', 'published')"

# The weighted FTS vector expression (RFC-002 §5.5, Appendix B `to_tsvector('simple', ...)`).
# Title is weight 'A', body 'B', so ``ts_rank`` ranks a title hit above a body-only hit — the
# P0.8 acceptance. **Must stay identical to the generated-column DDL in 0004_knowledge.py.**
_SEARCH_TSV = (
    "setweight(to_tsvector('simple', coalesce(title, '')), 'A') || "
    "setweight(to_tsvector('simple', coalesce(body_text, '')), 'B')"
)


class HelpCenter(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """Per-workspace Help Center configuration + theming. One row per workspace.

    Routing uses the workspace's own ``slug`` (identity.workspaces.slug); this table carries
    presentation + the (phase-2) ``custom_domain`` field.
    """

    __tablename__ = "help_centers"
    __table_args__ = (UniqueConstraint("workspace_id", name="uq_help_centers_workspace_id"),)

    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    logo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_color: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Custom domains ship in phase 2; the column exists now (RFC-000 §2.5 / P0.8 note).
    custom_domain: Mapped[str | None] = mapped_column(CITEXT, nullable=True)
    default_locale: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sa.text("'en'")
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Collection(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """A group of articles. ``slug`` is unique per workspace; ``parent_id`` allows nesting."""

    __tablename__ = "collections"
    __table_args__ = (
        UniqueConstraint("workspace_id", "slug", name="uq_collections_workspace_id_slug"),
    )

    slug: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    icon: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("0"))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("collections.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Article(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """A Help Center article. Block ``body`` (JSONB) + derived ``body_text`` → ``search_tsv``."""

    __tablename__ = "articles"
    __table_args__ = (CheckConstraint(_STATUS_CHECK, name="status_valid"),)

    collection_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("collections.id", ondelete="SET NULL"), nullable=True
    )
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    # Plaintext extracted from ``body`` at write time (blocks.blocks_to_text); the FTS source.
    body_text: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("''"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'draft'"))
    locale: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'en'"))
    seo_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    seo_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("0"))
    published_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    search_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR, sa.Computed(_SEARCH_TSV, persisted=True), nullable=True
    )


class ArticleTranslation(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """Per-locale translation of an article. Schema only in P0.8 (UI later, RFC-000 §2.5)."""

    __tablename__ = "article_translations"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "article_id", "locale", name="uq_article_translations_article_locale"
        ),
        CheckConstraint(_STATUS_CHECK, name="status_valid"),
    )

    article_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("articles.id", ondelete="CASCADE"), nullable=False
    )
    locale: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    body_text: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("''"))
    seo_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    seo_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'draft'"))
    published_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    search_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR, sa.Computed(_SEARCH_TSV, persisted=True), nullable=True
    )
