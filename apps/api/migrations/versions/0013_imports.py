"""imports: import_jobs + export_jobs

Revision ID: 0013_imports
Revises: 0012_segments
Create Date: 2026-07-24

P1.9 — CSV contact import/export job ledgers (RFC-002 §5.4). Both are tenant tables (RLS enabled +
FORCED via ``create_tenant_table``). Low volume (one row per admin-triggered job), so neither is in
LARGE_TABLES — plain ``op.create_index`` on the (empty) new tables is fine.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from relay.core.rls import create_tenant_table

revision: str = "0013_imports"
down_revision: str | None = "0012_segments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = pg.UUID(as_uuid=True)

_IMPORT_STATUS = "status IN ('pending', 'validating', 'processing', 'completed', 'failed')"
_EXPORT_STATUS = "status IN ('pending', 'processing', 'completed', 'failed')"


def _id_col() -> sa.Column:
    return sa.Column("id", _UUID, primary_key=True)


def _created_at_col() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def _workspace_fk() -> sa.Column:
    return sa.Column(
        "workspace_id", _UUID, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )


def _created_by_col() -> sa.Column:
    return sa.Column(
        "created_by", _UUID, sa.ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )


def upgrade() -> None:
    create_tenant_table(
        "import_jobs",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("kind", sa.Text(), nullable=False, server_default=sa.text("'contacts'")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("s3_key", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column(
            "column_mapping", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("total_rows", sa.Integer(), nullable=True),
        sa.Column("processed_rows", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("inserted_rows", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("updated_rows", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error_rows", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error_report_key", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        _created_by_col(),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_IMPORT_STATUS, name="import_status_valid"),
    )
    op.create_index("ix_import_jobs_ws_id", "import_jobs", ["workspace_id", "id"])

    create_tenant_table(
        "export_jobs",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("kind", sa.Text(), nullable=False, server_default=sa.text("'contacts'")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("filters", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("result_key", sa.Text(), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error", sa.Text(), nullable=True),
        _created_by_col(),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_EXPORT_STATUS, name="export_status_valid"),
    )
    op.create_index("ix_export_jobs_ws_id", "export_jobs", ["workspace_id", "id"])


def downgrade() -> None:
    op.drop_table("export_jobs")
    op.drop_table("import_jobs")
