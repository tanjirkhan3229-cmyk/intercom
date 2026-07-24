"""P1.8 outbound — migration applies, RLS isolates every new tenant table, ledgers dedupe.

Inserts one row into each new tenant table under workspace A, then proves workspace B (and an
unset ``app.ws``) see zero rows — the cross-tenant backstop RFC-002 §7 requires for every new
tenant table. Also proves the ``sends`` and ``post_receipts`` claim-slot uniques are hard.
"""

from __future__ import annotations

import datetime as dt
import uuid
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from relay.core.db import get_sessionmaker, session_scope
from relay.core.ids import IdPrefix, decode_public_id, uuid7
from relay.modules.crm.models import Contact
from relay.modules.outbound import models as m

pytestmark = pytest.mark.integration

PASSWORD = "password123"

# Every tenant table introduced by 0010_outbound (outbound_event_dedupe is global infra — no RLS).
_TENANT_MODELS = (
    m.SubscriptionType,
    m.Consent,
    m.ConsentEvent,
    m.Campaign,
    m.CampaignVersion,
    m.Send,
    m.CampaignStats,
    m.Post,
    m.PostReceipt,
    m.MessageEvent,
)


async def _owner(client: httpx.AsyncClient, ws_name: str) -> uuid.UUID:
    """Sign up an owner; return the workspace UUID."""
    resp = await client.post(
        "/v0/auth/signup",
        json={
            "workspace_name": ws_name,
            "email": f"owner-{uuid4().hex}@example.com",
            "password": PASSWORD,
            "name": "Owner",
        },
    )
    assert resp.status_code == 201
    return decode_public_id(IdPrefix.WORKSPACE, resp.json()["workspace"]["id"])


async def _seed_one_row_per_table(ws: uuid.UUID) -> None:
    """Insert exactly one row into each new tenant table under ``ws``'s GUC."""
    async with session_scope(ws) as s:
        contact = Contact(workspace_id=ws, kind="user", email=f"{uuid4().hex}@example.com")
        s.add(contact)
        await s.flush()

        # A distinct name — signup already seeds "Product updates"/"Transactional" defaults.
        subtype = m.SubscriptionType(workspace_id=ws, name="Weekly digest", kind="marketing")
        s.add(subtype)
        await s.flush()

        s.add(
            m.Consent(
                workspace_id=ws,
                contact_id=contact.id,
                subscription_type_id=subtype.id,
                state="subscribed",
                source="api",
            )
        )
        s.add(
            m.ConsentEvent(
                workspace_id=ws,
                contact_id=contact.id,
                subscription_type_id=subtype.id,
                to_state="subscribed",
                source="api",
                actor_kind="admin",
            )
        )

        campaign = m.Campaign(workspace_id=ws, name="Launch", subscription_type_id=subtype.id)
        s.add(campaign)
        await s.flush()
        version = m.CampaignVersion(
            workspace_id=ws,
            campaign_id=campaign.id,
            version=1,
            subject="Hi {{name}}",
            mjml="<mjml><mj-body>hello</mj-body></mjml>",
        )
        s.add(version)
        await s.flush()

        s.add(
            m.Send(
                workspace_id=ws,
                campaign_id=campaign.id,
                campaign_version_id=version.id,
                contact_id=contact.id,
                email=contact.email or "x@example.com",
                message_id=f"<{uuid4().hex}@relay>",
            )
        )
        s.add(m.CampaignStats(workspace_id=ws, campaign_id=campaign.id, audience_size=1))

        post = m.Post(workspace_id=ws, kind="post", title="News", body={"blocks": []})
        s.add(post)
        await s.flush()
        s.add(m.PostReceipt(workspace_id=ws, post_id=post.id, contact_id=contact.id))

        s.add(
            m.MessageEvent(
                id=uuid7(),
                workspace_id=ws,
                source_kind="email",
                source_id=campaign.id,
                campaign_id=campaign.id,
                contact_id=contact.id,
                event="sent",
                created_at=dt.datetime.now(dt.UTC),
            )
        )


async def test_migration_applies_and_rls_isolates_every_table(client: httpx.AsyncClient) -> None:
    ws_a = await _owner(client, "Alpha")
    ws_b = await _owner(client, "Bravo")

    await _seed_one_row_per_table(ws_a)

    # Under A's GUC every table has its row; under B's GUC none of A's rows are ever visible.
    # (B legitimately has its own signup-seeded subscription_types, so we check for A's rows
    # specifically rather than a bare count — RLS must make A's rows unreachable from B.)
    async with session_scope(ws_a) as sa, session_scope(ws_b) as sb:
        for model in _TENANT_MODELS:
            a_count = await sa.scalar(select(func.count()).select_from(model))
            a_rows_via_b = await sb.scalar(
                select(func.count()).select_from(model).where(model.workspace_id == ws_a)
            )
            assert a_count and a_count >= 1, f"{model.__tablename__} missing under owner"
            assert a_rows_via_b == 0, f"{model.__tablename__} leaked to another workspace"


async def test_unset_guc_returns_zero_rows(client: httpx.AsyncClient) -> None:
    ws_a = await _owner(client, "HasData")
    await _seed_one_row_per_table(ws_a)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        for model in _TENANT_MODELS:  # deliberately no app.ws set
            count = await session.scalar(select(func.count()).select_from(model))
            assert count == 0, f"{model.__tablename__} readable with no app.ws (FORCE RLS broken)"


async def test_sends_claim_slot_is_a_hard_unique(client: httpx.AsyncClient) -> None:
    ws = await _owner(client, "Claim")
    campaign_id, version_id, contact_id = uuid7(), uuid7(), uuid7()

    def _row() -> m.Send:
        return m.Send(
            workspace_id=ws,
            campaign_id=campaign_id,
            campaign_version_id=version_id,
            contact_id=contact_id,
            email="dup@example.com",
            message_id=f"<{uuid4().hex}@relay>",
        )

    async with session_scope(ws) as s:
        s.add(_row())

    # Second insert for the same (workspace, campaign, contact) must violate the claim-slot unique.
    with pytest.raises(IntegrityError):
        async with session_scope(ws) as s:
            s.add(_row())


async def test_post_receipt_claim_slot_is_a_hard_unique(client: httpx.AsyncClient) -> None:
    ws = await _owner(client, "PostClaim")
    async with session_scope(ws) as s:
        contact = Contact(workspace_id=ws, kind="user", email=f"{uuid4().hex}@example.com")
        post = m.Post(workspace_id=ws, kind="post", title="N", body={})
        s.add_all([contact, post])
        await s.flush()
        post_id, contact_id = post.id, contact.id
        s.add(m.PostReceipt(workspace_id=ws, post_id=post_id, contact_id=contact_id))

    with pytest.raises(IntegrityError):
        async with session_scope(ws) as s:
            s.add(m.PostReceipt(workspace_id=ws, post_id=post_id, contact_id=contact_id))
