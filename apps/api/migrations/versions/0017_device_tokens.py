"""mobile: device_tokens registry + push_receipts dedupe ledger

Revision ID: 0017_device_tokens
Revises: 0016_merge_automation_analytics
Create Date: 2026-07-24

P1.10 — Mobile SDKs beta. Two tenant tables (RLS enabled + FORCED via ``create_tenant_table``):

* ``device_tokens`` — APNs/FCM tokens registered by the iOS/Android SDKs for an end-user (contact).
  Registration is an upsert on ``(workspace_id, token)`` so token rotation just re-registers; the
  provider's invalid-token feedback (APNs 410, FCM NotRegistered) flips ``status`` to ``stale``.
* ``push_receipts`` — per-(message, device) dedupe ledger so the at-least-once push fan-out worker
  never double-sends a notification for the same conversation part (master rule #3).

Neither table is in LARGE_TABLES, so plain ``op.create_index`` is fine.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from relay.core.rls import create_tenant_table

revision: str = "0017_device_tokens"
down_revision: str | None = "0016_merge_automation_analytics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = pg.UUID(as_uuid=True)

_PLATFORM_CHECK = "platform IN ('ios', 'android')"
_ENV_CHECK = "environment IN ('production', 'sandbox')"
_STATUS_CHECK = "status IN ('active', 'stale')"


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


def upgrade() -> None:
    create_tenant_table(
        "device_tokens",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column(
            "contact_id", _UUID, sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("token", sa.Text(), nullable=False),
        # APNs bundle id / Android package name — routes to the right APNs topic; null → configured default.
        sa.Column("app_id", sa.Text(), nullable=True),
        sa.Column(
            "environment", sa.Text(), nullable=False, server_default=sa.text("'production'")
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(_PLATFORM_CHECK, name="device_token_platform_valid"),
        sa.CheckConstraint(_ENV_CHECK, name="device_token_environment_valid"),
        sa.CheckConstraint(_STATUS_CHECK, name="device_token_status_valid"),
        # A provider token is unique within a workspace; re-register upserts on it (rotation).
        sa.UniqueConstraint("workspace_id", "token", name="uq_device_tokens_token"),
    )
    # Fan-out lookup: active devices for a contact.
    op.create_index(
        "ix_device_tokens_contact",
        "device_tokens",
        ["workspace_id", "contact_id", "status"],
    )

    create_tenant_table(
        "push_receipts",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        # Plain uuid, NOT an FK: conversation_parts is partitioned with a composite PK
        # ``(created_at, id)``, so ``id`` alone is not a valid FK target (mirrors
        # ``email_messages.part_id``). It's only ever the dedupe key below.
        sa.Column("message_id", _UUID, nullable=False),
        sa.Column(
            "device_token_id",
            _UUID,
            sa.ForeignKey("device_tokens.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Provider receipt id (APNs apns-id / FCM message name), for support/debugging.
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "workspace_id", "message_id", "device_token_id", name="uq_push_receipts_dedupe"
        ),
    )


def downgrade() -> None:
    op.drop_table("push_receipts")
    op.drop_table("device_tokens")
