"""merge billing and knowledge heads

Revision ID: 0005_merge_billing_knowledge
Revises: 0004_billing, 0004_knowledge
Create Date: 2026-07-23 08:36:04.751021+00:00

Tenancy reminder (RFC-002 §7): create tenant-owned tables with
``relay.core.rls.create_tenant_table(...)`` so RLS is enabled + FORCED automatically.
Large-table indexes must be CREATE INDEX CONCURRENTLY inside an autocommit block
(enforced by scripts/check_migrations.py).
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0005_merge_billing_knowledge"
down_revision: str | Sequence[str] | None = ("0004_billing", "0004_knowledge")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
