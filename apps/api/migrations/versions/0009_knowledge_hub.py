"""knowledge hub + retrieval: external_sources, content_chunks, knowledge_settings, retrieval_evals

Revision ID: 0009_knowledge_hub
Revises: 0008_webhooks
Create Date: 2026-07-23

P1.1 - the retrieval substrate (RFC-002 §5.5 + Appendix B, RFC-003 §3-4). All four tables are
tenant tables (RLS enabled + FORCED via ``create_tenant_table``).

``content_chunks`` is in ``scripts/check_migrations.py`` LARGE_TABLES, so every secondary index
on it is built ``CREATE INDEX CONCURRENTLY`` inside an ``autocommit_block`` (the blessed pattern —
empty table here, but the linter enforces it). The three indexes per RFC-002 §5.5:
  - ``chunks_hnsw`` — HNSW over ``embedding halfvec_cosine_ops`` (raw SQL: the ``WITH (m, ef_*)``
    opclass form isn't expressible via ``op.create_index`` kwargs).
  - ``chunks_fts``  — GIN over the generated ``tsv``.
  - ``chunks_ws``   — ``(workspace_id, source_kind)`` btree (workspace_id-leading, RFC-002 §5.1).

RFC-002 §5.5 EXTENSION (documented in this change): the canonical DDL there is the retrieval
contract; P1.1 adds the ingestion/diffing columns ``chunk_index`` (stable ordinal) +
``content_hash`` (sha256 of content, so a re-sync re-embeds only changed chunks) +
``token_count``/``heading_path``/``title`` (budgeting + citations), and the practical upsert
key ``UNIQUE(workspace_id, source_kind, source_id, locale, emb_version, chunk_index)``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from relay.core.rls import create_tenant_table
from relay.modules.knowledge.vectors import EMBEDDING_DIM, Halfvec

revision: str = "0009_knowledge_hub"
down_revision: str | None = "0008_webhooks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = pg.UUID(as_uuid=True)


def _id_col() -> sa.Column:
    return sa.Column("id", _UUID, primary_key=True)


def _created_at_col() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def _updated_at_col() -> sa.Column:
    return sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )


def _workspace_fk() -> sa.Column:
    return sa.Column(
        "workspace_id", _UUID, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )


def upgrade() -> None:
    # --- knowledge_settings (per-workspace retrieval config; emb_version cutover point) ---------
    create_tenant_table(
        "knowledge_settings",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        _updated_at_col(),
        sa.Column("emb_version", sa.SmallInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column("ef_search", sa.Integer(), nullable=False, server_default=sa.text("100")),
        sa.UniqueConstraint("workspace_id", name="uq_knowledge_settings_workspace_id"),
    )

    # --- external_sources (url | pdf | snippet, with AI-readiness status) ----------------------
    create_tenant_table(
        "external_sources",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        _updated_at_col(),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("config", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("locale", sa.Text(), nullable=False, server_default=sa.text("'en'")),
        sa.Column("audience", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("content_hash", sa.Text(), nullable=True),
        sa.Column("document_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('url', 'pdf', 'snippet')", name="ck_external_sources_kind_valid"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'syncing', 'synced', 'error')",
            name="ck_external_sources_status_valid",
        ),
    )
    op.create_index("ix_external_sources_ws_status", "external_sources", ["workspace_id", "status"])

    # --- content_chunks (the retrievable, embedded unit — LARGE_TABLE) --------------------------
    create_tenant_table(
        "content_chunks",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        _updated_at_col(),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("source_id", _UUID, nullable=False),
        sa.Column("locale", sa.Text(), nullable=False, server_default=sa.text("'en'")),
        sa.Column("audience", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("heading_path", sa.Text(), nullable=True),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "tsv",
            pg.TSVECTOR(),
            sa.Computed("to_tsvector('simple', content)", persisted=True),
            nullable=True,
        ),
        sa.Column("embedding", Halfvec(EMBEDDING_DIM), nullable=True),
        sa.Column("emb_version", sa.SmallInteger(), nullable=False),
        sa.CheckConstraint(
            "source_kind IN ('article', 'pdf', 'url', 'snippet', 'custom_answer')",
            name="ck_content_chunks_source_kind_valid",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "source_kind",
            "source_id",
            "locale",
            "emb_version",
            "chunk_index",
            name="uq_content_chunks_identity",
        ),
    )
    # All secondary indexes on the LARGE_TABLE go CONCURRENTLY (linter-enforced). Empty here.
    with op.get_context().autocommit_block():
        op.create_index(
            "chunks_ws",
            "content_chunks",
            ["workspace_id", "source_kind"],
            postgresql_concurrently=True,
        )
        op.create_index(
            "chunks_fts",
            "content_chunks",
            ["tsv"],
            postgresql_using="gin",
            postgresql_concurrently=True,
        )
        # HNSW over halfvec cosine — the ANN index (RFC-002 §5.5). Raw SQL: the opclass +
        # WITH (m, ef_construction) form has no op.create_index kwarg equivalent.
        op.execute(
            "CREATE INDEX CONCURRENTLY chunks_hnsw ON content_chunks "
            "USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64)"
        )

    # --- retrieval_evals (CI retrieval-regression ledger; small table) --------------------------
    create_tenant_table(
        "retrieval_evals",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("corpus", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("k", sa.Integer(), nullable=False),
        sa.Column("recall_at_k", sa.Float(), nullable=False),
        sa.Column("mrr", sa.Float(), nullable=False),
        sa.Column("num_queries", sa.Integer(), nullable=False),
        sa.Column("emb_version", sa.SmallInteger(), nullable=False),
        sa.Column("params", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint(
            "method IN ('hybrid', 'vector', 'fts')", name="ck_retrieval_evals_method_valid"
        ),
    )
    op.create_index(
        "ix_retrieval_evals_ws_created", "retrieval_evals", ["workspace_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("retrieval_evals")
    op.drop_table("content_chunks")
    op.drop_table("external_sources")
    op.drop_table("knowledge_settings")
