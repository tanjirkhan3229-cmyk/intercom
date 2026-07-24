"""merge automation and neko_analytics heads

Revision ID: 0016_merge_automation_analytics
Revises: 0015_automation, 0015_neko_analytics
Create Date: 2026-07-24

P1.4 (neko_analytics) and P1.5 (automation) both branched off ``0014_integrations`` and merged to
main independently, leaving two alembic heads → ``alembic upgrade head`` errored with "multiple head
revisions". This is a no-op merge (mirrors ``0005_merge_billing_knowledge``) that reunites the chain
so downstream migrations (and the test-suite ``upgrade head``) have a single head again.
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0016_merge_automation_analytics"
down_revision: str | Sequence[str] | None = ("0015_automation", "0015_neko_analytics")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
