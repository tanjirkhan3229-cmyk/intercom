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
from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from relay.core.base_model import Base, TimestampMixin, UUIDPrimaryKey, WorkspaceScoped
from relay.modules.knowledge.vectors import EMBEDDING_DIM, Halfvec

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


# ===========================================================================================
# Knowledge Hub + retrieval substrate (P1.1, RFC-002 §5.5 + Appendix B, RFC-003 §3-4)
# ===========================================================================================

# --- Closed sets: text + CHECK (RFC-002 §5.1) ---------------------------------
# External source kinds we can ingest. ``custom_answer`` (admin-curated answers) lands with the
# AI agent (P1.3) but is a valid ``content_chunks.source_kind`` today, so keep the two sets split.
EXTERNAL_SOURCE_KINDS: tuple[str, ...] = ("url", "pdf", "snippet")
_SOURCE_KIND_CHECK = "kind IN ('url', 'pdf', 'snippet')"

# Per-source AI-readiness surfaced in the UI (RFC-000 §2.5 / P1.1 sources).
SOURCE_STATUSES: tuple[str, ...] = ("pending", "syncing", "synced", "error")
_SOURCE_STATUS_CHECK = "status IN ('pending', 'syncing', 'synced', 'error')"

# ``content_chunks.source_kind`` — the shredded provenance. Articles + the three external kinds
# + custom answers all funnel into one chunk table (RFC-003 §3 ingestion diagram).
CHUNK_SOURCE_KINDS: tuple[str, ...] = ("article", "pdf", "url", "snippet", "custom_answer")
_CHUNK_SOURCE_KIND_CHECK = "source_kind IN ('article', 'pdf', 'url', 'snippet', 'custom_answer')"

# Retrieval methods compared by the eval harness (hybrid must beat each single-signal baseline).
EVAL_METHODS: tuple[str, ...] = ("hybrid", "vector", "fts")
_EVAL_METHOD_CHECK = "method IN ('hybrid', 'vector', 'fts')"


class KnowledgeSettings(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """Per-workspace retrieval configuration. One row per workspace.

    ``emb_version`` is the **active** embedding version for this workspace's retrieval — the
    atomic cutover point of a re-embed migration (RFC-003 §4 "retrieval requires
    ``emb_version = current``"). A re-embed writes the new version's chunks alongside the old
    (dual-version), then flips this one column (per-workspace atomic cutover), then the old
    version's chunks are cleaned up. ``ef_search`` is the default HNSW oversampling knob;
    ``retrieve()`` may override it per call.
    """

    __tablename__ = "knowledge_settings"
    __table_args__ = (UniqueConstraint("workspace_id", name="uq_knowledge_settings_workspace_id"),)

    emb_version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=sa.text("1")
    )
    ef_search: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("100"))
    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ExternalSource(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """A synced knowledge source (url | pdf | snippet) with its AI-readiness status.

    ``config`` carries kind-specific settings (``url``/``sitemap`` for crawls, ``s3_key`` for a
    PDF, ``body`` for a snippet). ``content_hash`` is a whole-source digest used to short-circuit
    a re-sync that changed nothing; per-chunk diffing (only changed chunks re-embed) happens in
    the indexer against ``content_chunks.content_hash``.
    """

    __tablename__ = "external_sources"
    __table_args__ = (
        CheckConstraint(_SOURCE_KIND_CHECK, name="kind_valid"),
        CheckConstraint(_SOURCE_STATUS_CHECK, name="status_valid"),
    )

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'pending'"))
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    locale: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'en'"))
    audience: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    document_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa.text("0")
    )
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("0"))
    last_synced_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ContentChunk(UUIDPrimaryKey, WorkspaceScoped, Base):
    """A retrievable chunk — the shredded, embedded unit that hybrid retrieval reads.

    RFC-002 §5.5 fixes the core columns (``source_kind``/``source_id``/``locale``/``audience``/
    ``content``/``tsv``/``embedding halfvec(1536)``/``emb_version``). P1.1 **extends** the DDL
    (RFC-002 §5.5 updated in this change) with the identity + diffing columns the ingestion
    pipeline needs: ``chunk_index`` (stable ordinal within source+locale+emb_version) and
    ``content_hash`` (sha256 of ``content``) so a re-sync re-embeds **only** changed chunks, plus
    ``token_count``/``heading_path``/``title`` for budgeting and citations.

    The embedding is nullable: chunks are written first (fast, transactional with the source) and
    embedded in a batch pass, so a chunk can briefly exist un-embedded (excluded from retrieval,
    which requires a non-null vector at the current ``emb_version``).
    """

    __tablename__ = "content_chunks"
    __table_args__ = (
        CheckConstraint(_CHUNK_SOURCE_KIND_CHECK, name="source_kind_valid"),
        # Diff/upsert identity: one row per (source, locale, version, ordinal). The indexer
        # upserts on this and re-embeds only when ``content_hash`` changed.
        UniqueConstraint(
            "workspace_id",
            "source_kind",
            "source_id",
            "locale",
            "emb_version",
            "chunk_index",
            name="uq_content_chunks_identity",
        ),
    )

    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    locale: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'en'"))
    audience: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    heading_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("0"))
    tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        sa.Computed("to_tsvector('simple', content)", persisted=True),
        nullable=True,
    )
    embedding: Mapped[list[float] | None] = mapped_column(Halfvec(EMBEDDING_DIM), nullable=True)
    emb_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class RetrievalEval(UUIDPrimaryKey, TimestampMixin, WorkspaceScoped, Base):
    """One eval run's result for a (corpus, method) pair — the CI retrieval regression ledger.

    The harness (``knowledge.eval_harness``) ingests labeled synthetic corpora, runs hybrid /
    vector-only / FTS-only retrieval, and writes a row per method with recall@k + MRR. The gate
    asserts ``hybrid.recall_at_k`` clears the floor and beats both baselines (P1.1 acceptance).
    """

    __tablename__ = "retrieval_evals"
    __table_args__ = (CheckConstraint(_EVAL_METHOD_CHECK, name="method_valid"),)

    corpus: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    k: Mapped[int] = mapped_column(Integer, nullable=False)
    recall_at_k: Mapped[float] = mapped_column(sa.Float, nullable=False)
    mrr: Mapped[float] = mapped_column(sa.Float, nullable=False)
    num_queries: Mapped[int] = mapped_column(Integer, nullable=False)
    emb_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
