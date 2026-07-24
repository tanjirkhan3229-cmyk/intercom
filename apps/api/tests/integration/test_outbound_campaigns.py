"""P1.8 email broadcast pipeline — the acceptance criteria (RFC-000 §5 P1.8).

Drives the real service pipeline (fire → snapshot → per-recipient send → stats projection)
in-process against Postgres + Redis, asserting:
  #1 zero duplicates under concurrent workers AND on re-fire (the claim-slot ledger),
  #2 an unsubscribed contact is excluded at send time even if snapshotted while subscribed,
  #3 stats project + reconcile from message_events (SES engagement) with no drift,
  #4 a consent change mid-send is respected,
plus one-click List-Unsubscribe headers on the wire and bounce → suppression.

The real >=200/s throughput target is covered by the k6 load test (load/k6); here we prove logic.
"""

from __future__ import annotations

import asyncio
import email as emaillib
import uuid
from collections.abc import Iterator
from uuid import uuid4

import httpx
import psycopg
import pytest
from sqlalchemy import func, select

from relay.core import outbox_relay
from relay.core.db import session_scope
from relay.core.ids import IdPrefix, decode_public_id
from relay.core.redis import get_redis, get_redis_sync
from relay.modules.channels import sender
from relay.modules.outbound import service, stats_consumer
from relay.modules.outbound.models import Send
from relay.settings import get_settings

pytestmark = pytest.mark.integration

PASSWORD = "password123"


@pytest.fixture(autouse=True)
def _memory_transport() -> Iterator[None]:
    """Route outbound email through the in-process FakeSender (no Mailpit in tests)."""
    settings = get_settings()
    original = settings.email_transport
    settings.email_transport = "memory"  # type: ignore[misc]
    sender.reset_sender()
    sender.fake_sender().reset()
    yield
    settings.email_transport = original  # type: ignore[misc]
    sender.reset_sender()


async def _owner(client: httpx.AsyncClient, ws_name: str) -> tuple[str, uuid.UUID]:
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
    body = resp.json()
    return body["access_token"], decode_public_id(IdPrefix.WORKSPACE, body["workspace"]["id"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _marketing_type(client: httpx.AsyncClient, token: str) -> str:
    resp = await client.post(
        "/v0/outbound/subscription-types",
        json={"name": "Promos", "kind": "marketing"},
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _contacts(client: httpx.AsyncClient, token: str, n: int) -> list[str]:
    ids = []
    for i in range(n):
        resp = await client.post(
            "/v0/contacts/identify",
            json={"email": f"c{i}-{uuid4().hex}@example.com"},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        ids.append(resp.json()["id"])
    return ids


async def _campaign(
    client: httpx.AsyncClient, token: str, subtype_id: str, *, segment: dict | None = None
) -> str:
    resp = await client.post(
        "/v0/outbound/campaigns",
        json={
            "name": "Launch",
            "subscription_type_id": subtype_id,
            "segment": segment or {},
            "version": {
                "subject": "Hi {{ contact.name }}",
                "mjml": "<mjml><mj-body><mj-text>Hello</mj-text></mj-body></mjml>",
            },
        },
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _fire_and_snapshot(
    client: httpx.AsyncClient, token: str, ws: uuid.UUID, campaign_pub: str
) -> list[uuid.UUID]:
    """Fire via the API, then run the snapshot (the fire task) and return the queued contact ids."""
    fired = await client.post(f"/v0/outbound/campaigns/{campaign_pub}/fire", headers=_auth(token))
    assert fired.status_code == 200, fired.text
    campaign_id = decode_public_id(IdPrefix.CAMPAIGN, campaign_pub)
    collected: list[uuid.UUID] = []
    await service.run_fire_snapshot(ws, campaign_id, enqueue=collected.extend)
    return collected


def _drain_outbox() -> None:
    with psycopg.connect(get_settings().database_url_psycopg) as conn:
        conn.autocommit = False
        outbox_relay.drain(conn, get_redis_sync())


async def _project_stats() -> None:
    redis = get_redis()
    await stats_consumer.ensure_group(redis)
    _drain_outbox()
    while (await stats_consumer.consume_once(redis, from_id=">")).entries_read:
        pass


async def _stats(client: httpx.AsyncClient, token: str, campaign_pub: str) -> dict:
    resp = await client.get(f"/v0/outbound/campaigns/{campaign_pub}/stats", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _send_status_counts(ws: uuid.UUID, campaign_id: uuid.UUID) -> dict[str, int]:
    async with session_scope(ws) as s:
        rows = (
            (
                await s.execute(
                    select(Send.status, func.count())
                    .where(Send.campaign_id == campaign_id)
                    .group_by(Send.status)
                )
            )
            .tuples()
            .all()
        )
    return dict(rows)


# --- Happy path + List-Unsubscribe header ------------------------------------------------------


async def test_happy_path_sends_and_projects_stats(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "Happy")
    subtype = await _marketing_type(client, token)
    await _contacts(client, token, 5)
    campaign_pub = await _campaign(client, token, subtype)
    campaign_id = decode_public_id(IdPrefix.CAMPAIGN, campaign_pub)

    contacts = await _fire_and_snapshot(client, token, ws, campaign_pub)
    assert len(contacts) == 5
    for contact_id in contacts:
        assert (
            await service.send_one(workspace_id=ws, campaign_id=campaign_id, contact_id=contact_id)
        ) == "sent"

    # Every recipient got exactly one email; the wire carries one-click List-Unsubscribe headers.
    sent = sender.fake_sender().sent
    assert len(sent) == 5
    msg = emaillib.message_from_bytes(sent[0].raw)
    assert msg["List-Unsubscribe"] and msg["List-Unsubscribe"].startswith("<http")
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"

    await _project_stats()
    stats = await _stats(client, token, campaign_pub)
    assert stats["audience_size"] == 5 and stats["sent"] == 5


# --- Acceptance #1: zero duplicates (concurrent workers + re-fire) ------------------------------


async def test_zero_duplicates_concurrent_and_refire(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "Dedup")
    subtype = await _marketing_type(client, token)
    await _contacts(client, token, 1)
    campaign_pub = await _campaign(client, token, subtype)
    campaign_id = decode_public_id(IdPrefix.CAMPAIGN, campaign_pub)
    (contact_id,) = await _fire_and_snapshot(client, token, ws, campaign_pub)

    # Five concurrent workers race to send the same (campaign, contact): exactly one wins.
    results = await asyncio.gather(
        *[
            service.send_one(workspace_id=ws, campaign_id=campaign_id, contact_id=contact_id)
            for _ in range(5)
        ]
    )
    assert results.count("sent") == 1
    assert all(r == "already_processed" for r in results if r != "sent")
    assert len(sender.fake_sender().sent) == 1

    # Re-running the snapshot (a duplicate fire task) creates no new sends and sends nothing more.
    again = await service.run_fire_snapshot(ws, campaign_id, enqueue=lambda _ids: None)
    assert again == "already_snapshotted"
    assert (await _send_status_counts(ws, campaign_id)) == {"sent": 1}
    assert len(sender.fake_sender().sent) == 1

    # And the fire endpoint refuses to re-fire a completed/firing campaign.
    refire = await client.post(f"/v0/outbound/campaigns/{campaign_pub}/fire", headers=_auth(token))
    assert refire.status_code == 409


async def test_concurrent_sends_same_campaign_no_duplicates(client: httpx.AsyncClient) -> None:
    """Parallel workers sending DIFFERENT contacts of the SAME campaign must not collide on the
    outbox per-campaign seq (which would roll back a send after the provider already sent)."""
    token, ws = await _owner(client, "Concurrent")
    subtype = await _marketing_type(client, token)
    await _contacts(client, token, 6)
    campaign_pub = await _campaign(client, token, subtype)
    campaign_id = decode_public_id(IdPrefix.CAMPAIGN, campaign_pub)
    contacts = await _fire_and_snapshot(client, token, ws, campaign_pub)
    assert len(contacts) == 6

    results = await asyncio.gather(
        *[
            service.send_one(workspace_id=ws, campaign_id=campaign_id, contact_id=c)
            for c in contacts
        ],
        return_exceptions=True,
    )
    assert all(r == "sent" for r in results), results  # no IntegrityError, each sent once
    assert len(sender.fake_sender().sent) == 6
    assert (await _send_status_counts(ws, campaign_id)) == {"sent": 6}


# --- Acceptance #2 + #4: consent gates at send time --------------------------------------------


async def test_unsubscribed_excluded_at_send_time(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "UnsubGate")
    subtype = await _marketing_type(client, token)
    (contact_pub,) = await _contacts(client, token, 1)
    campaign_pub = await _campaign(client, token, subtype)
    campaign_id = decode_public_id(IdPrefix.CAMPAIGN, campaign_pub)
    (contact_id,) = await _fire_and_snapshot(client, token, ws, campaign_pub)

    # Contact was subscribed at snapshot; unsubscribe AFTER the snapshot, BEFORE the send.
    await client.put(
        f"/v0/outbound/contacts/{contact_pub}/consent",
        json={"subscription_type_id": subtype, "state": "unsubscribed"},
        headers=_auth(token),
    )
    result = await service.send_one(workspace_id=ws, campaign_id=campaign_id, contact_id=contact_id)
    assert result == "skipped:unsubscribed"
    assert sender.fake_sender().sent == []
    assert (await _send_status_counts(ws, campaign_id)) == {"skipped": 1}


async def test_consent_change_mid_send(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "MidSend")
    subtype = await _marketing_type(client, token)
    contacts_pub = await _contacts(client, token, 2)
    campaign_pub = await _campaign(client, token, subtype)
    campaign_id = decode_public_id(IdPrefix.CAMPAIGN, campaign_pub)
    contacts = await _fire_and_snapshot(client, token, ws, campaign_pub)

    # Send the first; flip the second's consent between sends; the second is then excluded.
    assert (
        await service.send_one(workspace_id=ws, campaign_id=campaign_id, contact_id=contacts[0])
    ) == "sent"
    # Map the remaining raw id back to its public id to unsubscribe via the API.
    second_pub = (
        contacts_pub[0]
        if decode_public_id(IdPrefix.CONTACT, contacts_pub[0]) == contacts[1]
        else contacts_pub[1]
    )
    await client.put(
        f"/v0/outbound/contacts/{second_pub}/consent",
        json={"subscription_type_id": subtype, "state": "unsubscribed"},
        headers=_auth(token),
    )
    assert (
        await service.send_one(workspace_id=ws, campaign_id=campaign_id, contact_id=contacts[1])
    ) == "skipped:unsubscribed"
    assert len(sender.fake_sender().sent) == 1


# --- Acceptance #3: engagement stats + reconcile -----------------------------------------------


async def test_engagement_stats_and_reconcile(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "Engage")
    subtype = await _marketing_type(client, token)
    await _contacts(client, token, 4)
    campaign_pub = await _campaign(client, token, subtype)
    campaign_id = decode_public_id(IdPrefix.CAMPAIGN, campaign_pub)
    contacts = await _fire_and_snapshot(client, token, ws, campaign_pub)
    for contact_id in contacts:
        await service.send_one(workspace_id=ws, campaign_id=campaign_id, contact_id=contact_id)

    # Provider ids assigned on send; drive SES-style engagement against them.
    async with session_scope(ws) as s:
        provider_ids = (
            await s.scalars(select(Send.provider_id).where(Send.campaign_id == campaign_id))
        ).all()
    provider_ids = [p for p in provider_ids if p]
    assert len(provider_ids) == 4

    for pid in provider_ids:  # all delivered
        await service.record_engagement_event(
            workspace_id=ws, provider_message_id=pid, event_kind="delivered"
        )
    for pid in provider_ids[:2]:  # two opened
        await service.record_engagement_event(
            workspace_id=ws, provider_message_id=pid, event_kind="open"
        )
    # A duplicate open for the same send must NOT double-count (unique-per-contact).
    dup = await service.record_engagement_event(
        workspace_id=ws, provider_message_id=provider_ids[0], event_kind="open"
    )
    assert dup == "duplicate"

    await _project_stats()
    stats = await _stats(client, token, campaign_pub)
    assert stats["sent"] == 4 and stats["delivered"] == 4 and stats["opened"] == 2

    # Reconcile recomputes from the message_events/sends ledgers → identical (the ±0.5% safety net).
    await service.reconcile_campaign_stats(ws, campaign_id)
    reconciled = await _stats(client, token, campaign_pub)
    assert reconciled["delivered"] == 4 and reconciled["opened"] == 2 and reconciled["sent"] == 4


async def test_bounce_suppresses_recipient(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "Bounce")
    subtype = await _marketing_type(client, token)
    await _contacts(client, token, 1)
    campaign_pub = await _campaign(client, token, subtype)
    campaign_id = decode_public_id(IdPrefix.CAMPAIGN, campaign_pub)
    (contact_id,) = await _fire_and_snapshot(client, token, ws, campaign_pub)
    await service.send_one(workspace_id=ws, campaign_id=campaign_id, contact_id=contact_id)

    async with session_scope(ws) as s:
        pid = await s.scalar(select(Send.provider_id).where(Send.campaign_id == campaign_id))
        email = await s.scalar(select(Send.email).where(Send.campaign_id == campaign_id))
    await service.record_engagement_event(
        workspace_id=ws, provider_message_id=pid, event_kind="bounce"
    )

    from relay.modules.channels import service as channels_service

    async with session_scope(ws) as s:
        assert await channels_service.is_suppressed(s, ws, email) is True


async def test_send_skips_soft_deleted_contact(client: httpx.AsyncClient) -> None:
    """A contact soft-deleted after the snapshot is never mailed (GDPR/erasure at send time)."""
    from sqlalchemy import update as sa_update

    from relay.modules.crm.models import Contact

    token, ws = await _owner(client, "DelSend")
    subtype = await _marketing_type(client, token)
    await _contacts(client, token, 1)
    campaign_pub = await _campaign(client, token, subtype)
    campaign_id = decode_public_id(IdPrefix.CAMPAIGN, campaign_pub)
    (contact_id,) = await _fire_and_snapshot(client, token, ws, campaign_pub)

    async with session_scope(ws) as s:
        await s.execute(
            sa_update(Contact).where(Contact.id == contact_id).values(deleted_at=func.now())
        )
    result = await service.send_one(workspace_id=ws, campaign_id=campaign_id, contact_id=contact_id)
    assert result == "skipped:contact_deleted"
    assert sender.fake_sender().sent == []


async def test_sweep_completes_campaign_and_reconciles(client: httpx.AsyncClient) -> None:
    """The periodic sweep flips a fully-sent campaign firing→sent and reconciles its stats."""
    from sqlalchemy import text as sa_text

    from relay.modules.outbound.models import Campaign

    token, ws = await _owner(client, "Sweep")
    subtype = await _marketing_type(client, token)
    await _contacts(client, token, 3)
    campaign_pub = await _campaign(client, token, subtype)
    campaign_id = decode_public_id(IdPrefix.CAMPAIGN, campaign_pub)
    contacts = await _fire_and_snapshot(client, token, ws, campaign_pub)
    for contact_id in contacts:
        await service.send_one(workspace_id=ws, campaign_id=campaign_id, contact_id=contact_id)

    async with session_scope(ws) as s:
        status_before = await s.scalar(select(Campaign.status).where(Campaign.id == campaign_id))
        await s.execute(sa_text("SELECT relay_outbound_sweep()"))  # SECURITY DEFINER sweep
    assert status_before == "firing"

    async with session_scope(ws) as s:
        status_after = await s.scalar(select(Campaign.status).where(Campaign.id == campaign_id))
    assert status_after == "sent"
    stats = await _stats(client, token, campaign_pub)
    assert stats["sent"] == 3  # reconciled from the sends ledger


async def test_duplicate_subscription_type_name_conflicts(client: httpx.AsyncClient) -> None:
    """Creating a subscription type whose name collides (incl. a seeded default) → 409, not 500."""
    token, _ws = await _owner(client, "DupType")
    resp = await client.post(
        "/v0/outbound/subscription-types",
        json={"name": "Product updates", "kind": "marketing"},  # a seeded default
        headers=_auth(token),
    )
    assert resp.status_code == 409, resp.text


async def test_ses_engagement_ingest_resolves_and_dedupes(client: httpx.AsyncClient) -> None:
    token, ws = await _owner(client, "SesIngest")
    subtype = await _marketing_type(client, token)
    await _contacts(client, token, 1)
    campaign_pub = await _campaign(client, token, subtype)
    campaign_id = decode_public_id(IdPrefix.CAMPAIGN, campaign_pub)
    (contact_id,) = await _fire_and_snapshot(client, token, ws, campaign_pub)
    await service.send_one(workspace_id=ws, campaign_id=campaign_id, contact_id=contact_id)
    async with session_scope(ws) as s:
        pid = await s.scalar(select(Send.provider_id).where(Send.campaign_id == campaign_id))

    # Pre-tenancy ingest resolves the workspace by SES MessageId and records the delivery.
    assert (
        await service.ingest_ses_engagement(
            provider_message_id=pid, ses_event_type="Delivery", sns_message_id="sns-1"
        )
        == "recorded"
    )
    # The same SNS delivery is deduped.
    assert (
        await service.ingest_ses_engagement(
            provider_message_id=pid, ses_event_type="Delivery", sns_message_id="sns-1"
        )
        == "duplicate_sns"
    )
    # An unknown provider id (e.g. an agent-reply bounce) is a no-op, not an error.
    assert (
        await service.ingest_ses_engagement(
            provider_message_id="no-such-id", ses_event_type="Open", sns_message_id="sns-2"
        )
        == "unresolved"
    )

    await _project_stats()
    stats = await _stats(client, token, campaign_pub)
    assert stats["delivered"] == 1
