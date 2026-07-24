"""outbound: campaigns/versions/sends, message_events (partitioned), campaign_stats,
subscription_types/consents/consent_events, posts/post_receipts, event dedupe

Revision ID: 0011_outbound
Revises: 0010_inbox_v2
Create Date: 2026-07-24

P1.8 — RFC-002 §5.6 (outbound tables), RFC-001 §6.7 (campaign fire), RFC-000 §2.6.

Tenancy: every table below except ``outbound_event_dedupe`` is a tenant table (RLS enabled + FORCED
via ``create_tenant_table``). ``outbound_event_dedupe`` is global infra (no workspace_id / no RLS,
like ``outbox``/``channels_inbound_dedupe``): the SES/SNS webhook resolves the workspace *after*
this dedupe gate, so an RLS-forced role with no ``app.ws`` set could never read it.

Index strategy vs the migration linter (scripts/check_migrations.py):
- ``sends`` (**LARGE_TABLES**, non-partitioned — see models.py for why it is not partitioned): its
  ``UNIQUE(workspace_id, campaign_id, contact_id)`` claim slot is declared inline on the table (part
  of CREATE TABLE on an empty table; the linter only scans ``op.create_index``/``op.execute``), and
  its secondary indexes are built ``CONCURRENTLY`` inside an ``autocommit_block`` (the blessed
  large-table pattern, mirroring ``contacts`` in 0002_crm).
- ``message_events`` (**LARGE_TABLES**, *partitioned*): ``CREATE INDEX CONCURRENTLY`` is unsupported
  on a partitioned parent, so its indexes are inline partitioned *templates* on ``create_table``
  (mirrors ``webhook_deliveries`` in 0008 / ``events`` in 0002). Partitions are seeded current..T+2
  months via ``relay_ensure_partitions`` (from 0002_crm); the ``housekeeping`` beat keeps ahead.
- All other tables are small/regular: plain ``op.create_index``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from relay.core.rls import create_tenant_table

revision: str = "0011_outbound"
down_revision: str | None = "0010_inbox_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = pg.UUID(as_uuid=True)

# Enum value lists kept in lockstep with modules/outbound/models.py CHECK constraints.
_CAMPAIGN_STATUSES = "'draft', 'scheduled', 'firing', 'sent', 'paused', 'cancelled', 'failed'"
_VERSION_STATUSES = "'draft', 'published', 'archived'"
_SEND_STATUSES = "'queued', 'sending', 'sent', 'skipped', 'failed'"
_SKIP_REASONS = (
    "'suppressed', 'unsubscribed', 'no_consent', 'freq_capped', "
    "'no_email', 'contact_deleted', 'paused'"
)
_SUB_KINDS = "'marketing', 'transactional'"
_CONSENT_STATES = "'subscribed', 'unsubscribed'"
_CONSENT_SOURCES = (
    "'import', 'api', 'admin', 'list_unsubscribe', "
    "'unsubscribe_page', 'double_opt_in', 'bounce_complaint'"
)
_CONSENT_ACTORS = "'contact', 'admin', 'system'"
_POST_KINDS = "'post', 'chat'"
_MESSAGE_EVENT_SOURCES = "'email', 'post', 'chat'"
_MESSAGE_EVENT_KINDS = (
    "'sent', 'delivered', 'open', 'click', 'bounce', "
    "'complaint', 'unsub', 'seen', 'failed', 'suppressed'"
)
_POST_RECEIPT_STATES = (
    "'pending', 'delivered', 'seen', 'clicked', 'suppressed_consent', 'suppressed_hard', 'skipped'"
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


def upgrade() -> None:
    # --- subscription_types (small, regular) ---
    create_tenant_table(
        "subscription_types",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False, server_default=sa.text("'marketing'")),
        sa.Column("requires_opt_in", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("workspace_id", "name", name="uq_subscription_types_workspace_id_name"),
        sa.CheckConstraint(f"kind IN ({_SUB_KINDS})", name="ck_subscription_types_kind_valid"),
    )
    op.create_index("ix_subscription_types_ws_id", "subscription_types", ["workspace_id", "id"])

    # --- consents (small, regular) — current-state projection ---
    create_tenant_table(
        "consents",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column(
            "contact_id", _UUID, sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "subscription_type_id",
            _UUID,
            sa.ForeignKey("subscription_types.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("last_event_id", _UUID, nullable=True),
        _updated_at_col(),
        sa.UniqueConstraint(
            "workspace_id",
            "contact_id",
            "subscription_type_id",
            name="uq_consents_workspace_id_contact_id_subscription_type_id",
        ),
        sa.CheckConstraint(f"state IN ({_CONSENT_STATES})", name="ck_consents_state_valid"),
        sa.CheckConstraint(f"source IN ({_CONSENT_SOURCES})", name="ck_consents_source_valid"),
    )

    # --- consent_events (small, regular) — append-only audit ---
    create_tenant_table(
        "consent_events",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column(
            "contact_id", _UUID, sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "subscription_type_id",
            _UUID,
            sa.ForeignKey("subscription_types.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("from_state", sa.Text(), nullable=True),
        sa.Column("to_state", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("actor_kind", sa.Text(), nullable=True),
        sa.Column("actor_id", _UUID, nullable=True),
        sa.Column("campaign_id", _UUID, nullable=True),
        sa.Column("detail", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint(
            f"from_state IS NULL OR from_state IN ({_CONSENT_STATES})",
            name="ck_consent_events_from_state_valid",
        ),
        sa.CheckConstraint(
            f"to_state IN ({_CONSENT_STATES})", name="ck_consent_events_to_state_valid"
        ),
        sa.CheckConstraint(
            f"source IN ({_CONSENT_SOURCES})", name="ck_consent_events_source_valid"
        ),
        sa.CheckConstraint(
            f"actor_kind IS NULL OR actor_kind IN ({_CONSENT_ACTORS})",
            name="ck_consent_events_actor_kind_valid",
        ),
    )
    # btree scans backward for the "latest events first" audit read; no DESC needed.
    op.create_index(
        "ix_consent_events_ws_contact_type_created",
        "consent_events",
        ["workspace_id", "contact_id", "subscription_type_id", "created_at"],
    )

    # --- campaigns (small, regular) ---
    create_tenant_table(
        "campaigns",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False, server_default=sa.text("'email'")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("segment", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "subscription_type_id",
            _UUID,
            sa.ForeignKey("subscription_types.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("active_version_id", _UUID, nullable=True),
        sa.Column("fired_version_id", _UUID, nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("snapshot_done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by", _UUID, sa.ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
        ),
        _updated_at_col(),
        sa.CheckConstraint("channel IN ('email')", name="ck_campaigns_channel_valid"),
        sa.CheckConstraint(f"status IN ({_CAMPAIGN_STATUSES})", name="ck_campaigns_status_valid"),
    )
    op.create_index("ix_campaigns_ws_id", "campaigns", ["workspace_id", "id"])
    op.create_index("ix_campaigns_ws_status", "campaigns", ["workspace_id", "status"])

    # --- campaign_versions (small, regular) ---
    create_tenant_table(
        "campaign_versions",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column(
            "campaign_id",
            _UUID,
            sa.ForeignKey("campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("preheader", sa.Text(), nullable=True),
        sa.Column("mjml", sa.Text(), nullable=False),
        sa.Column("from_name", sa.Text(), nullable=True),
        sa.Column("reply_to", sa.Text(), nullable=True),
        sa.Column("variables", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("graph", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'draft'")),
        sa.Column(
            "created_by", _UUID, sa.ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "campaign_id",
            "version",
            name="uq_campaign_versions_workspace_id_campaign_id_version",
        ),
        sa.CheckConstraint(
            f"status IN ({_VERSION_STATUSES})", name="ck_campaign_versions_status_valid"
        ),
    )

    # --- sends (LARGE, regular/non-partitioned) — claim slot inline, secondary idx CONCURRENTLY ---
    create_tenant_table(
        "sends",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("campaign_id", _UUID, nullable=False),
        sa.Column("campaign_version_id", _UUID, nullable=False),
        sa.Column("contact_id", _UUID, nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("provider_id", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "workspace_id",
            "campaign_id",
            "contact_id",
            name="uq_sends_workspace_id_campaign_id_contact_id",
        ),
        sa.CheckConstraint(f"status IN ({_SEND_STATUSES})", name="ck_sends_status_valid"),
        sa.CheckConstraint(
            f"skip_reason IS NULL OR skip_reason IN ({_SKIP_REASONS})",
            name="ck_sends_skip_reason_valid",
        ),
    )

    # --- campaign_stats (small, regular) — rollup projection ---
    create_tenant_table(
        "campaign_stats",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("campaign_id", _UUID, nullable=False),
        sa.Column("audience_size", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("sent", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("delivered", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("opened", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("clicked", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("bounced", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("complained", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("unsubscribed", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("skipped", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failed", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_seq", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        _updated_at_col(),
        sa.UniqueConstraint(
            "workspace_id", "campaign_id", name="uq_campaign_stats_workspace_id_campaign_id"
        ),
    )

    # --- posts (small, regular) ---
    create_tenant_table(
        "posts",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("kind", sa.Text(), nullable=False, server_default=sa.text("'post'")),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("body", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("segment", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "subscription_type_id",
            _UUID,
            sa.ForeignKey("subscription_types.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("snapshot_done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("audience_size", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_by", _UUID, sa.ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
        ),
        _updated_at_col(),
        sa.CheckConstraint(f"kind IN ({_POST_KINDS})", name="ck_posts_kind_valid"),
        sa.CheckConstraint(f"status IN ({_CAMPAIGN_STATUSES})", name="ck_posts_status_valid"),
    )
    op.create_index("ix_posts_ws_id", "posts", ["workspace_id", "id"])
    op.create_index("ix_posts_ws_status", "posts", ["workspace_id", "status"])

    # --- post_receipts (regular; NOT a LARGE_TABLE) — claim slot inline ---
    create_tenant_table(
        "post_receipts",
        _id_col(),
        _workspace_fk(),
        _created_at_col(),
        sa.Column("post_id", _UUID, sa.ForeignKey("posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "contact_id", _UUID, sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("conversation_id", _UUID, nullable=True),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("clicked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "workspace_id",
            "post_id",
            "contact_id",
            name="uq_post_receipts_workspace_id_post_id_contact_id",
        ),
        sa.CheckConstraint(
            f"state IN ({_POST_RECEIPT_STATES})", name="ck_post_receipts_state_valid"
        ),
    )
    op.create_index(
        "ix_post_receipts_post_state", "post_receipts", ["workspace_id", "post_id", "state"]
    )

    # --- message_events (LARGE, partitioned) — inline index templates ---
    create_tenant_table(
        "message_events",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("workspace_id", _UUID, nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("source_id", _UUID, nullable=False),
        sa.Column("campaign_id", _UUID, nullable=True),
        sa.Column("contact_id", _UUID, nullable=True),
        sa.Column("email", pg.CITEXT(), nullable=True),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("provider_id", sa.Text(), nullable=True),
        sa.Column("provider_event_id", sa.Text(), nullable=True),
        sa.Column("detail", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("created_at", "id", name="pk_message_events"),
        sa.UniqueConstraint(
            "created_at",
            "workspace_id",
            "provider_id",
            "event",
            "provider_event_id",
            name="uq_message_events_dedupe",
        ),
        sa.Index("message_events_rollup", "workspace_id", "campaign_id", "event"),
        sa.Index("message_events_source", "workspace_id", "source_kind", "source_id"),
        sa.CheckConstraint(
            f"source_kind IN ({_MESSAGE_EVENT_SOURCES})", name="ck_message_events_source_kind_valid"
        ),
        sa.CheckConstraint(
            f"event IN ({_MESSAGE_EVENT_KINDS})", name="ck_message_events_event_valid"
        ),
        postgresql_partition_by="RANGE (created_at)",
    )
    op.execute("SELECT relay_ensure_partitions('message_events', 2)")

    # --- outbound_event_dedupe (GLOBAL infra: no workspace_id / no RLS) ---
    op.create_table(
        "outbound_event_dedupe",
        sa.Column("sns_message_id", sa.Text(), primary_key=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # Pre-tenancy resolver for SES engagement webhooks: map an SES MessageId (== sends.provider_id)
    # to its owning workspace. SECURITY DEFINER (owned by BYPASSRLS migrator) because the webhook
    # runs before the workspace is known; EXECUTE-granted to app_rw (mirrors channels resolvers).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION relay_outbound_resolve_send(pid text)
        RETURNS TABLE(workspace_id uuid) LANGUAGE sql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $fn$
            SELECT workspace_id FROM public.sends WHERE provider_id = pid LIMIT 1
        $fn$;
        REVOKE ALL ON FUNCTION relay_outbound_resolve_send(text) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION relay_outbound_resolve_send(text) TO app_rw;
        """
    )

    # Periodic sweep (called by the outbound.sweep_campaigns beat task via app_rw): (1) reconcile
    # every still-`firing` campaign's stats from the source-of-truth ledgers (the ±0.5% net behind
    # the streamed projection), then (2) flip `firing`→`sent` for campaigns/posts with no pending
    # work left. SECURITY DEFINER (BYPASSRLS migrator) so one workspace-agnostic sweep covers all
    # tenants without a runtime BYPASSRLS role. Returns the number of campaigns completed.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION relay_outbound_sweep()
        RETURNS int LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $fn$
        DECLARE completed int := 0;
        BEGIN
            UPDATE public.campaign_stats cs SET
                sent = (SELECT count(*) FROM public.sends s
                        WHERE s.campaign_id = cs.campaign_id AND s.status = 'sent'),
                skipped = (SELECT count(*) FROM public.sends s
                           WHERE s.campaign_id = cs.campaign_id AND s.status = 'skipped'),
                failed = (SELECT count(*) FROM public.sends s
                          WHERE s.campaign_id = cs.campaign_id AND s.status = 'failed'),
                delivered = (SELECT count(DISTINCT me.contact_id) FROM public.message_events me
                             WHERE me.campaign_id = cs.campaign_id AND me.event = 'delivered'),
                opened = (SELECT count(DISTINCT me.contact_id) FROM public.message_events me
                          WHERE me.campaign_id = cs.campaign_id AND me.event = 'open'),
                clicked = (SELECT count(DISTINCT me.contact_id) FROM public.message_events me
                           WHERE me.campaign_id = cs.campaign_id AND me.event = 'click'),
                bounced = (SELECT count(DISTINCT me.contact_id) FROM public.message_events me
                           WHERE me.campaign_id = cs.campaign_id AND me.event = 'bounce'),
                complained = (SELECT count(DISTINCT me.contact_id) FROM public.message_events me
                              WHERE me.campaign_id = cs.campaign_id AND me.event = 'complaint'),
                unsubscribed = (SELECT count(DISTINCT me.contact_id) FROM public.message_events me
                                WHERE me.campaign_id = cs.campaign_id AND me.event = 'unsub'),
                updated_at = now()
            FROM public.campaigns c
            WHERE c.id = cs.campaign_id AND c.status = 'firing';

            UPDATE public.campaigns SET status = 'sent', updated_at = now()
            WHERE status = 'firing' AND snapshot_done_at IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM public.sends s
                              WHERE s.campaign_id = campaigns.id
                                AND s.status IN ('queued', 'sending'));
            GET DIAGNOSTICS completed = ROW_COUNT;

            UPDATE public.posts SET status = 'sent', updated_at = now()
            WHERE status = 'firing' AND snapshot_done_at IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM public.post_receipts r
                              WHERE r.post_id = posts.id AND r.state = 'pending');
            RETURN completed;
        END $fn$;
        REVOKE ALL ON FUNCTION relay_outbound_sweep() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION relay_outbound_sweep() TO app_rw;
        """
    )

    # --- sends secondary indexes: CONCURRENTLY (LARGE_TABLES; linter-enforced) ---
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_sends_ws_campaign_status",
            "sends",
            ["workspace_id", "campaign_id", "status"],
            postgresql_concurrently=True,
        )
        op.create_index(
            "ix_sends_ws_provider_id",
            "sends",
            ["workspace_id", "provider_id"],
            postgresql_where=sa.text("provider_id IS NOT NULL"),
            postgresql_concurrently=True,
        )
        op.create_index(
            "ix_sends_ws_id",
            "sends",
            ["workspace_id", "id"],
            postgresql_concurrently=True,
        )
        # Global provider-id lookup for the pre-tenancy engagement resolver (provider_id is unique
        # per send in practice; partial excludes the many queued rows without one).
        op.create_index(
            "ix_sends_provider_lookup",
            "sends",
            ["provider_id"],
            postgresql_where=sa.text("provider_id IS NOT NULL"),
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS relay_outbound_sweep()")
    op.execute("DROP FUNCTION IF EXISTS relay_outbound_resolve_send(text)")
    op.execute("DROP TABLE IF EXISTS message_events CASCADE")  # drops partitions too
    op.drop_table("outbound_event_dedupe")
    op.drop_table("post_receipts")
    op.drop_table("posts")
    op.drop_table("campaign_stats")
    op.drop_table("sends")
    op.drop_table("campaign_versions")
    op.drop_table("campaigns")
    op.drop_table("consent_events")
    op.drop_table("consents")
    op.drop_table("subscription_types")
