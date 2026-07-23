"""channels: email adapter — verified domains, channel accounts, email ledger, suppressions,
              delivery events + inbound dedupe/DLQ infra + SECURITY DEFINER routing resolvers

Revision ID: 0006_channels
Revises: 0005_merge_billing_knowledge
Create Date: 2026-07-23

RFC-002 §5.6 (channel tables) + RFC-001 §6.6 (email topology) + P0.7.

Tenancy:
- ``verified_domains``, ``channel_accounts``, ``email_messages``, ``suppressions``,
  ``email_delivery_events`` are tenant tables — RLS enabled + FORCED via ``create_tenant_table``.
- ``channels_inbound_dedupe`` and ``channels_ingest_failures`` are **infrastructure** (like
  ``outbox``): read by workers *before* the workspace is known (inbound routing / DLQ), so they
  carry no RLS. Isolation for them is that only workers touch them.

Pre-tenancy routing uses SECURITY DEFINER functions owned by the BYPASSRLS ``migrator`` (mirrors
``identity_admin_workspaces``): the ingest worker has no ``app.ws`` set when it must map an inbound
recipient address / In-Reply-To message-id to a workspace.

Expand step: ``conversations.channel_account_id`` gains its FK to ``channel_accounts`` here (0003
deliberately deferred it to P0.7). Added ``NOT VALID`` then ``VALIDATE`` — existing rows are all
NULL so validation is instant and lock-light.

Deviation (recorded in RFC-002 §5.6): P0.7 uses a non-partitioned ``email_delivery_events`` for the
low-volume email delivery audit; the partitioned ``message_events`` (campaign scale) stays with the
outbound module (P1.8). No ``reply_tokens`` table — reply tokens are stateless HMAC (RFC-001 §6.6
"plus-addressed reply tokens"; no RFC mandates a table).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from relay.core.rls import create_tenant_table

revision: str = "0006_channels"
down_revision: str | None = "0005_merge_billing_knowledge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = pg.UUID(as_uuid=True)


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


# SECURITY DEFINER routing resolvers (owned by BYPASSRLS migrator; EXECUTE granted to app_rw).
# The inbound worker calls these with no ``app.ws`` set, so RLS would otherwise hide the rows.
_RESOLVERS = r"""
CREATE FUNCTION channels_resolve_inbound_address(addr citext)
RETURNS TABLE(workspace_id uuid, channel_account_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $fn$
    SELECT ca.workspace_id, ca.id
    FROM channel_accounts ca
    WHERE ca.address = addr AND ca.status = 'active'
    LIMIT 1
$fn$;
REVOKE ALL ON FUNCTION channels_resolve_inbound_address(citext) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION channels_resolve_inbound_address(citext) TO app_rw;

CREATE FUNCTION channels_resolve_outbound_message(mid text)
RETURNS TABLE(workspace_id uuid, conversation_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $fn$
    SELECT em.workspace_id, em.conversation_id
    FROM email_messages em
    WHERE em.message_id = mid AND em.direction = 'out'
    LIMIT 1
$fn$;
REVOKE ALL ON FUNCTION channels_resolve_outbound_message(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION channels_resolve_outbound_message(text) TO app_rw;

CREATE FUNCTION channels_pending_domains()
RETURNS TABLE(workspace_id uuid, id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $fn$
    SELECT vd.workspace_id, vd.id
    FROM verified_domains vd
    WHERE vd.status = 'pending'
$fn$;
REVOKE ALL ON FUNCTION channels_pending_domains() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION channels_pending_domains() TO app_rw;

-- Status-AGNOSTIC account→workspace lookup for SES event attribution (bounce/complaint).
-- channels_resolve_inbound_address is active-only (delivery routing); a bounce/complaint must
-- suppress the recipient even when the sending account is paused/disabled (compliance).
CREATE FUNCTION channels_resolve_account_workspace(addr citext)
RETURNS TABLE(workspace_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $fn$
    SELECT ca.workspace_id
    FROM channel_accounts ca
    WHERE ca.address = addr
    LIMIT 1
$fn$;
REVOKE ALL ON FUNCTION channels_resolve_account_workspace(citext) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION channels_resolve_account_workspace(citext) TO app_rw;
"""


def upgrade() -> None:
    # --- verified_domains (tenant) ---
    create_tenant_table(
        "verified_domains",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("domain", pg.CITEXT(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("dkim_tokens", pg.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("spf_ok", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("dmarc_ok", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("dns_records", pg.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("verification_token", sa.Text(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "workspace_id", "domain", name="uq_verified_domains_workspace_id_domain"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'verified', 'failed')", name="ck_verified_domains_status_valid"
        ),
    )
    # GLOBAL routing determinism: a domain can be *verified* by only one workspace. Enforced
    # beneath RLS by the storage layer. (Tenant B's collision error is masked at the service
    # layer to avoid leaking that another tenant owns the domain.)
    op.create_index(
        "uq_verified_domains_verified_global",
        "verified_domains",
        ["domain"],
        unique=True,
        postgresql_where=sa.text("status = 'verified'"),
    )

    # --- channel_accounts (tenant) ---
    create_tenant_table(
        "channel_accounts",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("channel", sa.Text(), nullable=False, server_default=sa.text("'email'")),
        sa.Column("address", pg.CITEXT(), nullable=False),
        sa.Column(
            "domain_id",
            _UUID,
            sa.ForeignKey("verified_domains.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("settings", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        # Global-unique inbound address (routing key across tenants).
        sa.UniqueConstraint("address", name="uq_channel_accounts_address"),
        sa.CheckConstraint("channel IN ('email')", name="ck_channel_accounts_channel_valid"),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'disabled')", name="ck_channel_accounts_status_valid"
        ),
    )
    op.create_index("ix_channel_accounts_workspace_id", "channel_accounts", ["workspace_id"])

    # --- email_messages (tenant): dedupe + threading + outbound exactly-once gate ---
    create_tenant_table(
        "email_messages",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column(
            "conversation_id",
            _UUID,
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("part_id", _UUID, nullable=True),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("in_reply_to", sa.Text(), nullable=True),
        sa.Column("email_references", pg.ARRAY(sa.Text()), nullable=True),
        sa.Column("s3_raw_key", sa.Text(), nullable=True),
        sa.Column("from_addr", sa.Text(), nullable=True),
        sa.Column("to_addr", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "workspace_id", "message_id", name="uq_email_messages_workspace_id_message_id"
        ),
        sa.UniqueConstraint(
            "workspace_id", "part_id", name="uq_email_messages_workspace_id_part_id"
        ),
        sa.CheckConstraint("direction IN ('in', 'out')", name="ck_email_messages_direction_valid"),
    )
    op.create_index("ix_email_messages_conv", "email_messages", ["workspace_id", "conversation_id"])

    # --- suppressions (tenant) ---
    create_tenant_table(
        "suppressions",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("email", pg.CITEXT(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("detail", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.UniqueConstraint("workspace_id", "email", name="uq_suppressions_workspace_id_email"),
        sa.CheckConstraint(
            "reason IN ('bounce', 'complaint', 'manual')", name="ck_suppressions_reason_valid"
        ),
    )

    # --- email_delivery_events (tenant; non-partitioned for P0.7 — see module note) ---
    create_tenant_table(
        "email_delivery_events",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("part_id", _UUID, nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("detail", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint(
            "event IN ('sent', 'delivered', 'bounce', 'complaint', 'blocked', 'failed')",
            name="ck_email_delivery_events_event_valid",
        ),
    )
    op.create_index(
        "ix_email_delivery_events_workspace_id_part_id",
        "email_delivery_events",
        ["workspace_id", "part_id"],
    )

    # --- channels_inbound_dedupe (infra; NO RLS) — SNS MessageId idempotency gate ---
    op.create_table(
        "channels_inbound_dedupe",
        sa.Column("sns_message_id", sa.Text(), primary_key=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # --- channels_ingest_failures (infra; NO RLS) — DLQ replay log ---
    op.create_table(
        "channels_ingest_failures",
        _id_col(),
        _created_at_col(),
        sa.Column("workspace_id", _UUID, nullable=True),
        sa.Column("sns_message_id", sa.Text(), nullable=True),
        sa.Column("s3_bucket", sa.Text(), nullable=True),
        sa.Column("s3_key", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("detail", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )

    # --- expand: conversations.channel_account_id FK (0003 deferred it to P0.7) ---
    op.execute(
        "ALTER TABLE conversations ADD CONSTRAINT "
        "fk_conversations_channel_account_id_channel_accounts "
        "FOREIGN KEY (channel_account_id) REFERENCES channel_accounts(id) "
        "ON DELETE SET NULL NOT VALID"
    )
    op.execute(
        "ALTER TABLE conversations VALIDATE CONSTRAINT "
        "fk_conversations_channel_account_id_channel_accounts"
    )

    # --- pre-tenancy routing resolvers (SECURITY DEFINER) ---
    op.execute(_RESOLVERS)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS channels_resolve_account_workspace(citext)")
    op.execute("DROP FUNCTION IF EXISTS channels_pending_domains()")
    op.execute("DROP FUNCTION IF EXISTS channels_resolve_outbound_message(text)")
    op.execute("DROP FUNCTION IF EXISTS channels_resolve_inbound_address(citext)")
    op.execute(
        "ALTER TABLE conversations DROP CONSTRAINT IF EXISTS "
        "fk_conversations_channel_account_id_channel_accounts"
    )
    op.drop_table("channels_ingest_failures")
    op.drop_table("channels_inbound_dedupe")
    op.drop_table("email_delivery_events")
    op.drop_table("suppressions")
    op.drop_table("email_messages")
    op.drop_table("channel_accounts")
    op.drop_index("uq_verified_domains_verified_global", table_name="verified_domains")
    op.drop_table("verified_domains")
