"""knowledge: help_centers, collections, articles (FTS), article_translations

Revision ID: 0004_knowledge
Revises: 0003_messaging
Create Date: 2026-07-23

RFC-000 §2.5 (Help Center) + RFC-002 §5.5. P0.8 scope — the Help Center content model.
``content_chunks`` / embeddings / HNSW (RFC-002 §5.5) are P1.1 and deliberately NOT here.

All four tables are tenant tables (RLS enabled + FORCED via ``create_tenant_table``). None of
them are in the migration linter's ``LARGE_TABLES`` set (scripts/check_migrations.py) — they
are small config/content tables — so their indexes build normally (no CONCURRENTLY needed);
they are also brand-new + empty here, so there is no lock concern.

``articles.search_tsv`` / ``article_translations.search_tsv`` are **generated** tsvectors with
title weighted 'A' above body 'B', so ``ts_rank`` orders a title match above a body-only match
(P0.8 FTS acceptance). The expression MUST stay identical to ``_SEARCH_TSV`` in
relay/modules/knowledge/models.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from relay.core.rls import create_tenant_table

revision: str = "0004_knowledge"
down_revision: str | None = "0003_messaging"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = pg.UUID(as_uuid=True)

# MUST match relay/modules/knowledge/models.py `_SEARCH_TSV`.
_SEARCH_TSV_SQL = (
    "setweight(to_tsvector('simple', coalesce(title, '')), 'A') || "
    "setweight(to_tsvector('simple', coalesce(body_text, '')), 'B')"
)


def _id_col() -> sa.Column:
    return sa.Column("id", _UUID, primary_key=True)


def _created_at_col() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def _updated_at_col() -> sa.Column:
    return sa.Column(
        "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def _workspace_fk() -> sa.Column:
    return sa.Column(
        "workspace_id", _UUID, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )


def _search_tsv_col() -> sa.Column:
    return sa.Column(
        "search_tsv",
        pg.TSVECTOR(),
        sa.Computed(_SEARCH_TSV_SQL, persisted=True),
        nullable=True,
    )


def upgrade() -> None:
    # --- help_centers (one per workspace: theming + phase-2 custom_domain field) ---
    create_tenant_table(
        "help_centers",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("logo_url", sa.Text(), nullable=True),
        sa.Column("primary_color", sa.Text(), nullable=True),
        sa.Column("custom_domain", pg.CITEXT(), nullable=True),  # unused until phase 2
        sa.Column("default_locale", sa.Text(), nullable=False, server_default=sa.text("'en'")),
        _updated_at_col(),
        sa.UniqueConstraint("workspace_id", name="uq_help_centers_workspace_id"),
    )
    # Custom domains are globally unique when set (phase-2 routing). Partial unique index.
    op.create_index(
        "help_centers_custom_domain",
        "help_centers",
        ["custom_domain"],
        unique=True,
        postgresql_where=sa.text("custom_domain IS NOT NULL"),
    )

    # --- collections (article groupings; self-referential parent_id for nesting) ---
    create_tenant_table(
        "collections",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("icon", sa.Text(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "parent_id", _UUID, sa.ForeignKey("collections.id", ondelete="SET NULL"), nullable=True
        ),
        _updated_at_col(),
        sa.UniqueConstraint("workspace_id", "slug", name="uq_collections_workspace_id_slug"),
    )
    op.create_index("collections_ws_position", "collections", ["workspace_id", "position"])

    # --- articles (block body + generated weighted FTS vector) ---
    create_tenant_table(
        "articles",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column(
            "collection_id",
            _UUID,
            sa.ForeignKey("collections.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("body_text", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("locale", sa.Text(), nullable=False, server_default=sa.text("'en'")),
        sa.Column("seo_title", sa.Text(), nullable=True),
        sa.Column("seo_description", sa.Text(), nullable=True),
        sa.Column(
            "author_id", _UUID, sa.ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("position", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        _updated_at_col(),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        _search_tsv_col(),
        sa.CheckConstraint("status IN ('draft', 'published')", name="ck_articles_status_valid"),
    )
    # Slug unique per workspace among live rows; also serves the public slug lookup.
    op.create_index(
        "articles_slug",
        "articles",
        ["workspace_id", "slug"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    # FTS (R7/R8). Bare gin(search_tsv), mirroring messaging's parts_fts.
    op.create_index("articles_search", "articles", ["search_tsv"], postgresql_using="gin")
    # Collection listing + per-collection counts.
    op.create_index("articles_collection", "articles", ["workspace_id", "collection_id"])
    # Admin keyset listing over live rows (ORDER BY id DESC).
    op.create_index(
        "articles_ws_active",
        "articles",
        ["workspace_id", "id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # --- article_translations (schema only in P0.8; UI later — RFC-000 §2.5) ---
    create_tenant_table(
        "article_translations",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column(
            "article_id", _UUID, sa.ForeignKey("articles.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("locale", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("body_text", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("seo_title", sa.Text(), nullable=True),
        sa.Column("seo_description", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        _updated_at_col(),
        _search_tsv_col(),
        sa.UniqueConstraint(
            "workspace_id", "article_id", "locale", name="uq_article_translations_article_locale"
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'published')", name="ck_article_translations_status_valid"
        ),
    )
    op.create_index(
        "article_translations_search",
        "article_translations",
        ["search_tsv"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_article_translations_article",
        "article_translations",
        ["workspace_id", "article_id"],
    )


def downgrade() -> None:
    op.drop_table("article_translations")
    op.drop_table("articles")
    op.drop_table("collections")
    op.drop_table("help_centers")
