"""messaging: conversations, conversation_parts (partitioned), tags, saved_replies
              + the consistency spine (outbox, idempotency_keys)

Revision ID: 0003_messaging
Revises: 0002_crm
Create Date: 2026-07-23

RFC-002 §5.3 (messaging core) + §5.6 (outbox, idempotency_keys) + RFC-001 §6.5.

Tenancy:
- ``conversations``, ``conversation_parts``, ``conversation_tags``, ``saved_replies``,
  ``idempotency_keys`` are tenant tables — RLS enabled + FORCED via ``create_tenant_table``.
- ``outbox`` is **not** a tenant table (RFC-002 §5.6 lists no ``workspace_id``): it is
  infrastructure the single relay must read across all workspaces, so it has no RLS. The
  owning workspace travels in ``payload``. Only the relay reads it; request paths only append.

Index strategy vs the migration linter (scripts/check_migrations.py):
- ``conversations`` (large): partial indexes built ``CONCURRENTLY`` in an autocommit block.
- ``conversation_parts`` (large, *partitioned*): CONCURRENTLY is unsupported on a partitioned
  parent, so its indexes are inline partitioned index *templates* on ``create_table`` (emitted
  by the DDL compiler, not ``op.create_index``, so the linter does not flag them — and there is
  no lock concern on a brand-new empty table). Mirrors ``events`` in 0002_crm.

Deviations from the §5.3 sketch (which is "illustrative, not exhaustive", §5.2): a nullable
``conversation_parts.meta`` JSONB is added so assignment / state_change / rating parts are
self-describing (the timeline in P0.5 and reporting in P0.9 read it) without a second table;
and ``channel_account_id`` carries no FK yet (``channel_accounts`` lands with the channels
module in P0.7 — the FK is added then, expand/contract).

Partition functions (``relay_ensure_partitions`` etc.) were created in 0002_crm; this migration
reuses them to seed ``conversation_parts`` partitions current..T+2 months.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from relay.core.rls import create_tenant_table

revision: str = "0003_messaging"
down_revision: str | None = "0002_crm"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = pg.UUID(as_uuid=True)
_STATE_ENUM = pg.ENUM("open", "snoozed", "closed", name="conversation_state", create_type=False)


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


# Purge expired idempotency keys. SECURITY DEFINER (owned by the BYPASSRLS migrator) because a
# workspace-agnostic housekeeping sweep can't run under RLS as app_rw; EXECUTE-granted to app_rw
# so the housekeeping task can call it without DDL rights. Mirrors the partition-function pattern.
_PURGE_FUNCTION = r"""
CREATE OR REPLACE FUNCTION relay_purge_expired_idempotency_keys()
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $fn$
DECLARE deleted bigint;
BEGIN
    DELETE FROM public.idempotency_keys WHERE expires_at < now();
    GET DIAGNOSTICS deleted = ROW_COUNT;
    RETURN deleted;
END;
$fn$;
REVOKE ALL ON FUNCTION relay_purge_expired_idempotency_keys() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION relay_purge_expired_idempotency_keys() TO app_rw;
"""


def upgrade() -> None:
    op.execute("CREATE TYPE conversation_state AS ENUM ('open', 'snoozed', 'closed')")

    # --- conversations (large; fillfactor 85 for HOT-update headroom) ---
    create_tenant_table(
        "conversations",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column(
            "contact_id", _UUID, sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("channel", sa.Text(), nullable=False, server_default=sa.text("'chat'")),
        sa.Column("channel_account_id", _UUID, nullable=True),  # FK added in P0.7
        sa.Column("state", _STATE_ENUM, nullable=False, server_default=sa.text("'open'")),
        sa.Column(
            "assignee_id", _UUID, sa.ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("team_id", _UUID, sa.ForeignKey("teams.id", ondelete="SET NULL"), nullable=True),
        sa.Column("priority", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("waiting_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_part_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_contact_reply_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attributes", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("ai_status", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "state <> 'snoozed' OR snoozed_until IS NOT NULL", name="ck_conversations_snooze_shape"
        ),
    )
    # HOT-update headroom: the head row is updated on every part (RFC-002 §5.3/§9).
    op.execute("ALTER TABLE conversations SET (fillfactor = 85)")

    # --- conversation_parts (large, partitioned firehose) — inline index templates ---
    create_tenant_table(
        "conversation_parts",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("workspace_id", _UUID, nullable=False),
        sa.Column("conversation_id", _UUID, nullable=False),
        sa.Column("author_kind", sa.Text(), nullable=False),
        sa.Column("author_id", _UUID, nullable=True),
        sa.Column("part_type", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column(
            "body_tsv",
            pg.TSVECTOR(),
            sa.Computed("to_tsvector('simple', coalesce(body, ''))", persisted=True),
            nullable=True,
        ),
        sa.Column("attachments", pg.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column(
            "channel_meta", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("meta", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("created_at", "id", name="pk_conversation_parts"),
        sa.Index("parts_thread", "conversation_id", "id"),  # R2 keyset
        sa.Index("parts_fts", "body_tsv", postgresql_using="gin"),  # R8 (per partition)
        postgresql_partition_by="RANGE (created_at)",
    )
    op.execute("SELECT relay_ensure_partitions('conversation_parts', 2)")

    # --- conversation_tags ---
    create_tenant_table(
        "conversation_tags",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column(
            "conversation_id",
            _UUID,
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "workspace_id", "conversation_id", "name", name="uq_conversation_tags_conv_name"
        ),
    )
    op.create_index(
        "ix_conversation_tags_conv", "conversation_tags", ["workspace_id", "conversation_id"]
    )

    # --- saved_replies (macros) ---
    create_tenant_table(
        "saved_replies",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("shortcut", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.UniqueConstraint("workspace_id", "shortcut", name="uq_saved_replies_shortcut"),
    )

    # --- outbox (infrastructure; NO workspace_id / NO RLS — RFC-002 §5.6) ---
    op.create_table(
        "outbox",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("aggregate", sa.Text(), nullable=False),
        sa.Column("aggregate_id", _UUID, nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("payload", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("aggregate_id", "seq", name="uq_outbox_aggregate_id_seq"),
    )
    # The relay scans only unpublished rows; this partial index keeps that hot as rows churn.
    op.create_index(
        "outbox_pending",
        "outbox",
        ["aggregate_id", "seq"],
        postgresql_where=sa.text("published_at IS NULL"),
    )

    # --- idempotency_keys (tenant table; RLS) ---
    create_tenant_table(
        "idempotency_keys",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("response", pg.JSONB(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "key", name="uq_idempotency_keys_workspace_id_key"),
    )
    op.create_index("ix_idempotency_keys_expires_at", "idempotency_keys", ["expires_at"])
    op.execute(_PURGE_FUNCTION)

    # --- conversations partial indexes: CONCURRENTLY (large table; linter-enforced) ---
    with op.get_context().autocommit_block():
        # R1 (the money query): open convos for a team, ordered by waiting_since. The partial
        # predicate MUST match the query's `state='open'` exactly (guarded by an EXPLAIN test).
        op.create_index(
            "conv_open_team",
            "conversations",
            ["workspace_id", "team_id", "waiting_since"],
            postgresql_where=sa.text("state = 'open'"),
            postgresql_concurrently=True,
        )
        op.create_index(
            "conv_open_asgn",
            "conversations",
            ["workspace_id", "assignee_id", "waiting_since"],
            postgresql_where=sa.text("state = 'open'"),
            postgresql_concurrently=True,
        )
        # R3 contact panel: a contact's conversations, newest activity first.
        op.create_index(
            "conv_contact",
            "conversations",
            ["workspace_id", "contact_id", sa.text("last_part_at DESC")],
            postgresql_concurrently=True,
        )
        # Wake scan for snoozed conversations (beat timer). Bare snoozed_until per RFC-002 §5.3.
        op.create_index(
            "conv_snoozed",
            "conversations",
            ["snoozed_until"],
            postgresql_where=sa.text("state = 'snoozed'"),
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS relay_purge_expired_idempotency_keys()")
    op.drop_table("idempotency_keys")
    op.drop_index("outbox_pending", table_name="outbox")
    op.drop_table("outbox")
    op.drop_table("saved_replies")
    op.drop_table("conversation_tags")
    op.execute("DROP TABLE IF EXISTS conversation_parts CASCADE")  # drops partitions too
    op.drop_table("conversations")
    op.execute("DROP TYPE IF EXISTS conversation_state")
