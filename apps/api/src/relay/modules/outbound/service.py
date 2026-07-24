"""Service layer for the ``outbound`` module (P1.8) — the cross-module interface.

This file holds subscription types, consent (current-state projection + append-only audit), and the
public unsubscribe path. Email broadcast (campaign/version/fire/send) and in-app post/chat logic are
appended in later sections of the module. Reaching into another module's ``models``/``router`` is
forbidden (import-linter); cross-module work goes through ``crm.service`` / ``channels.service`` /
``messaging.service`` and domain events on the outbox.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from relay.core import outbox
from relay.core.db import session_scope
from relay.core.errors import ConflictError, NotFoundError, RateLimitedError, ValidationError
from relay.core.ids import IdPrefix, decode_public_id, encode_public_id, uuid7
from relay.core.logging import get_logger
from relay.core.principal import Principal
from relay.core.rbac import Role, authorize
from relay.modules.channels import service as channels_service
from relay.modules.crm import service as crm_service
from relay.modules.messaging import service as messaging_service
from relay.settings import get_settings

from . import events, mjml, ratelimit, schemas
from .models import (
    Campaign,
    CampaignStats,
    CampaignVersion,
    Consent,
    ConsentEvent,
    MessageEvent,
    OutboundEventDedupe,
    Post,
    PostReceipt,
    Send,
    SubscriptionType,
)
from .unsubscribe_token import make_unsubscribe_token

log = get_logger(__name__)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# --- DTO builders ------------------------------------------------------------------------------


def subscription_type_out(t: SubscriptionType) -> schemas.SubscriptionTypeOut:
    return schemas.SubscriptionTypeOut(
        id=encode_public_id(IdPrefix.SUBSCRIPTION_TYPE, t.id),
        name=t.name,
        description=t.description,
        kind=t.kind,
        requires_opt_in=t.requires_opt_in,
        archived_at=t.archived_at,
        created_at=t.created_at,
    )


def consent_out(c: Consent) -> schemas.ConsentOut:
    return schemas.ConsentOut(
        id=encode_public_id(IdPrefix.CONSENT, c.id),
        contact_id=encode_public_id(IdPrefix.CONTACT, c.contact_id),
        subscription_type_id=encode_public_id(IdPrefix.SUBSCRIPTION_TYPE, c.subscription_type_id),
        state=c.state,
        source=c.source,
        updated_at=c.updated_at,
        created_at=c.created_at,
    )


def _decode_or_404(prefix: str, public_id: str, what: str) -> uuid.UUID:
    try:
        return decode_public_id(prefix, public_id)
    except ValueError as exc:
        raise NotFoundError(f"{what} not found") from exc


# --- Subscription types ------------------------------------------------------------------------


async def _get_subscription_type(
    session: AsyncSession, subscription_type_id: uuid.UUID
) -> SubscriptionType:
    row = await session.get(SubscriptionType, subscription_type_id)
    if row is None:
        raise NotFoundError("subscription type not found")
    return row


async def create_subscription_type(
    session: AsyncSession, principal: Principal, req: schemas.SubscriptionTypeCreate
) -> schemas.SubscriptionTypeOut:
    authorize(principal, min_role=Role.ADMIN)
    row = SubscriptionType(
        workspace_id=principal.workspace_id,
        name=req.name,
        description=req.description,
        kind=req.kind,
        requires_opt_in=req.requires_opt_in,
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:  # UNIQUE(workspace_id, name) — incl. the seeded defaults
        raise ConflictError("a subscription type with this name already exists") from exc
    return subscription_type_out(row)


async def list_subscription_types(
    session: AsyncSession, principal: Principal, *, include_archived: bool = False
) -> list[schemas.SubscriptionTypeOut]:
    stmt = select(SubscriptionType).order_by(SubscriptionType.created_at)
    if not include_archived:
        stmt = stmt.where(SubscriptionType.archived_at.is_(None))
    rows = (await session.scalars(stmt)).all()
    return [subscription_type_out(r) for r in rows]


async def archive_subscription_type(
    session: AsyncSession, principal: Principal, public_id: str
) -> None:
    authorize(principal, min_role=Role.ADMIN)
    row = await _get_subscription_type(
        session, _decode_or_404(IdPrefix.SUBSCRIPTION_TYPE, public_id, "subscription type")
    )
    if row.archived_at is None:
        row.archived_at = _now()
        await session.flush()


# Names seeded into every new workspace so consent has categories from day one.
_DEFAULT_SUBSCRIPTION_TYPES: tuple[tuple[str, str, str, bool], ...] = (
    ("Product updates", "News, announcements and product education.", "marketing", False),
    ("Transactional", "Receipts, security and account notifications.", "transactional", False),
)


async def seed_default_subscription_types(session: AsyncSession, workspace_id: uuid.UUID) -> None:
    """Create the default subscription types for a freshly-provisioned workspace (system path, no
    RBAC — called by identity signup under the new workspace's GUC). Idempotent by name."""
    existing = set(
        (
            await session.scalars(
                select(SubscriptionType.name).where(SubscriptionType.workspace_id == workspace_id)
            )
        ).all()
    )
    for name, description, kind, requires_opt_in in _DEFAULT_SUBSCRIPTION_TYPES:
        if name in existing:
            continue
        session.add(
            SubscriptionType(
                workspace_id=workspace_id,
                name=name,
                description=description,
                kind=kind,
                requires_opt_in=requires_opt_in,
            )
        )
    await session.flush()


# --- Consent (current-state projection + append-only audit) ------------------------------------


async def set_consent(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    contact_id: uuid.UUID,
    subscription_type_id: uuid.UUID,
    state: str,
    source: str,
    actor_kind: str,
    actor_id: uuid.UUID | None = None,
    campaign_id: uuid.UUID | None = None,
    detail: dict[str, Any] | None = None,
) -> Consent:
    """Record a consent change: append a ``consent_events`` audit row and upsert the ``consents``
    projection, then emit ``outbound.consent.changed`` — all in the caller's transaction (master
    rule 2). The projection gives the fast send-time lookup; the audit row is the immutable record.
    """
    current = (
        await session.execute(
            select(Consent)
            .where(
                Consent.contact_id == contact_id,
                Consent.subscription_type_id == subscription_type_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    from_state = current.state if current is not None else None

    event = ConsentEvent(
        workspace_id=workspace_id,
        contact_id=contact_id,
        subscription_type_id=subscription_type_id,
        from_state=from_state,
        to_state=state,
        source=source,
        actor_kind=actor_kind,
        actor_id=actor_id,
        campaign_id=campaign_id,
        detail=detail or {},
    )
    session.add(event)
    await session.flush()

    if current is None:
        current = Consent(
            workspace_id=workspace_id,
            contact_id=contact_id,
            subscription_type_id=subscription_type_id,
            state=state,
            source=source,
            last_event_id=event.id,
        )
        session.add(current)
        await session.flush()
    else:
        current.state = state
        current.source = source
        current.last_event_id = event.id
        current.updated_at = _now()

    await outbox.emit(
        session,
        aggregate=events.AGGREGATE_CONSENT,
        aggregate_id=current.id,
        topic=events.CONSENT_CHANGED,
        payload={
            "workspace_id": encode_public_id(IdPrefix.WORKSPACE, workspace_id),
            "contact_id": encode_public_id(IdPrefix.CONTACT, contact_id),
            "subscription_type_id": encode_public_id(
                IdPrefix.SUBSCRIPTION_TYPE, subscription_type_id
            ),
            "state": state,
            "source": source,
            "occurred_at": _now().isoformat(),
        },
    )
    return current


async def set_consent_admin(
    session: AsyncSession, principal: Principal, contact_public_id: str, req: schemas.ConsentSetIn
) -> schemas.ConsentOut:
    authorize(principal, min_role=Role.ADMIN)
    contact_id = _decode_or_404(IdPrefix.CONTACT, contact_public_id, "contact")
    subscription_type_id = _decode_or_404(
        IdPrefix.SUBSCRIPTION_TYPE, req.subscription_type_id, "subscription type"
    )
    # Ensure the type exists in this workspace (RLS-scoped) — a bad id must 404, not FK-error.
    await _get_subscription_type(session, subscription_type_id)
    consent = await set_consent(
        session,
        workspace_id=principal.workspace_id,
        contact_id=contact_id,
        subscription_type_id=subscription_type_id,
        state=req.state,
        source="admin",
        actor_kind="admin",
        actor_id=principal.admin_id,
    )
    return consent_out(consent)


async def list_consents(
    session: AsyncSession, principal: Principal, contact_public_id: str
) -> list[schemas.ConsentOut]:
    contact_id = _decode_or_404(IdPrefix.CONTACT, contact_public_id, "contact")
    rows = (
        await session.scalars(
            select(Consent).where(Consent.contact_id == contact_id).order_by(Consent.created_at)
        )
    ).all()
    return [consent_out(r) for r in rows]


async def get_consent_state(
    session: AsyncSession, contact_id: uuid.UUID, subscription_type_id: uuid.UUID
) -> str | None:
    """Current consent state (subscribed/unsubscribed) for the pair, or ``None`` if unset."""
    state: str | None = await session.scalar(
        select(Consent.state).where(
            Consent.contact_id == contact_id,
            Consent.subscription_type_id == subscription_type_id,
        )
    )
    return state


async def is_blocked_by_consent(
    session: AsyncSession,
    *,
    contact_id: uuid.UUID,
    subscription_type: SubscriptionType,
) -> bool:
    """Send-time consent gate for a marketing type. Transactional types are never blocked.

    Opt-out model (default): a contact is blocked only when an explicit ``unsubscribed`` row exists.
    If the type ``requires_opt_in`` (GDPR), absence of a ``subscribed`` row blocks instead.
    """
    if subscription_type.kind == "transactional":
        return False
    state = await get_consent_state(session, contact_id, subscription_type.id)
    if subscription_type.requires_opt_in:
        return state != "subscribed"
    return state == "unsubscribed"


# --- Public unsubscribe (RFC 8058 one-click) ---------------------------------------------------


async def describe_unsubscribe(
    session: AsyncSession, subscription_type_id: uuid.UUID
) -> str | None:
    """Return the subscription type's display name for the confirmation page (no state change).

    ``None`` if the type no longer exists — the endpoint then shows a neutral page.
    """
    subtype = await session.get(SubscriptionType, subscription_type_id)
    return subtype.name if subtype is not None else None


async def apply_unsubscribe(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    contact_id: uuid.UUID,
    subscription_type_id: uuid.UUID,
    source: str,
    detail: dict[str, Any] | None = None,
    campaign_id: uuid.UUID | None = None,
) -> str | None:
    """Set consent to ``unsubscribed`` for one (contact, type) from a token-authenticated request.

    Returns the subscription type's display name (for the confirmation page), or ``None`` if the
    type/contact no longer exists (a hard-deleted contact's FK cascade already removed consents).
    Idempotent: a repeat click writes another audit row but the projection stays ``unsubscribed``.
    """
    subtype = await session.get(SubscriptionType, subscription_type_id)
    if subtype is None:
        return None
    try:
        await set_consent(
            session,
            workspace_id=workspace_id,
            contact_id=contact_id,
            subscription_type_id=subscription_type_id,
            state="unsubscribed",
            source=source,
            actor_kind="contact",
            campaign_id=campaign_id,
            detail=detail,
        )
    except ValidationError:
        return None
    return subtype.name


# --- Email broadcasts: campaigns + versions ----------------------------------------------------


def campaign_out(c: Campaign) -> schemas.CampaignOut:
    return schemas.CampaignOut(
        id=encode_public_id(IdPrefix.CAMPAIGN, c.id),
        name=c.name,
        channel=c.channel,
        status=c.status,
        subscription_type_id=(
            encode_public_id(IdPrefix.SUBSCRIPTION_TYPE, c.subscription_type_id)
            if c.subscription_type_id
            else None
        ),
        segment=c.segment,
        active_version_id=(
            encode_public_id(IdPrefix.CAMPAIGN_VERSION, c.active_version_id)
            if c.active_version_id
            else None
        ),
        fired_at=c.fired_at,
        created_at=c.created_at,
    )


def campaign_version_out(v: CampaignVersion) -> schemas.CampaignVersionOut:
    return schemas.CampaignVersionOut(
        id=encode_public_id(IdPrefix.CAMPAIGN_VERSION, v.id),
        version=v.version,
        subject=v.subject,
        preheader=v.preheader,
        from_name=v.from_name,
        reply_to=v.reply_to,
        status=v.status,
        created_at=v.created_at,
    )


def campaign_stats_out(s: CampaignStats) -> schemas.CampaignStatsOut:
    return schemas.CampaignStatsOut(
        campaign_id=encode_public_id(IdPrefix.CAMPAIGN, s.campaign_id),
        audience_size=s.audience_size,
        sent=s.sent,
        delivered=s.delivered,
        opened=s.opened,
        clicked=s.clicked,
        bounced=s.bounced,
        complained=s.complained,
        unsubscribed=s.unsubscribed,
        skipped=s.skipped,
        failed=s.failed,
    )


async def _get_campaign(session: AsyncSession, campaign_id: uuid.UUID) -> Campaign:
    row = await session.get(Campaign, campaign_id)
    if row is None:
        raise NotFoundError("campaign not found")
    return row


async def create_campaign(
    session: AsyncSession, principal: Principal, req: schemas.CampaignCreate
) -> schemas.CampaignOut:
    authorize(principal, min_role=Role.ADMIN)
    # Validate the audience predicate against the contact schema now, so a broken segment is
    # rejected at author time rather than at fire time (empty segment = all contacts).
    await crm_service.validate_contact_audience(session, req.segment)

    subscription_type_id: uuid.UUID | None = None
    if req.subscription_type_id is not None:
        subscription_type_id = _decode_or_404(
            IdPrefix.SUBSCRIPTION_TYPE, req.subscription_type_id, "subscription type"
        )
        await _get_subscription_type(session, subscription_type_id)

    campaign = Campaign(
        workspace_id=principal.workspace_id,
        name=req.name,
        subscription_type_id=subscription_type_id,
        segment=req.segment,
        created_by=principal.admin_id,
    )
    session.add(campaign)
    await session.flush()

    version = CampaignVersion(
        workspace_id=principal.workspace_id,
        campaign_id=campaign.id,
        version=1,
        subject=req.version.subject,
        preheader=req.version.preheader,
        mjml=req.version.mjml,
        from_name=req.version.from_name,
        reply_to=req.version.reply_to,
        variables=req.version.variables,
        status="published",
        created_by=principal.admin_id,
    )
    session.add(version)
    await session.flush()
    campaign.active_version_id = version.id
    await session.flush()
    return campaign_out(campaign)


async def get_campaign(session: AsyncSession, public_id: str) -> schemas.CampaignOut:
    return campaign_out(
        await _get_campaign(session, _decode_or_404(IdPrefix.CAMPAIGN, public_id, "campaign"))
    )


async def list_campaigns(session: AsyncSession) -> list[schemas.CampaignOut]:
    rows = (await session.scalars(select(Campaign).order_by(Campaign.created_at.desc()))).all()
    return [campaign_out(r) for r in rows]


async def get_campaign_stats(session: AsyncSession, public_id: str) -> schemas.CampaignStatsOut:
    campaign_id = _decode_or_404(IdPrefix.CAMPAIGN, public_id, "campaign")
    await _get_campaign(session, campaign_id)
    row = await session.scalar(
        select(CampaignStats).where(CampaignStats.campaign_id == campaign_id)
    )
    if row is None:  # not fired yet — return a zeroed view
        return schemas.CampaignStatsOut(
            campaign_id=public_id,
            audience_size=0,
            sent=0,
            delivered=0,
            opened=0,
            clicked=0,
            bounced=0,
            complained=0,
            unsubscribed=0,
            skipped=0,
            failed=0,
        )
    return campaign_stats_out(row)


# --- Fire (request path): pin version, transition state, emit campaign.fired --------------------

_FIREABLE = ("draft", "scheduled")


async def fire_campaign(
    session: AsyncSession, principal: Principal, public_id: str
) -> schemas.CampaignOut:
    """Begin a broadcast: pin the version, move to ``firing``, ensure a stats row, and emit
    ``campaign.fired`` in the same txn (the fire consumer does the snapshot + chunked enqueue).
    Idempotent at the endpoint via ``@idempotent``; a second fire is rejected by the state guard.
    """
    authorize(principal, min_role=Role.ADMIN)
    campaign = await _get_campaign(
        session, _decode_or_404(IdPrefix.CAMPAIGN, public_id, "campaign")
    )
    if campaign.status not in _FIREABLE:
        raise ConflictError(f"campaign is not in a fireable state (status={campaign.status})")
    if campaign.active_version_id is None:
        raise ValidationError("campaign has no version to send")

    campaign.status = "firing"
    campaign.fired_version_id = campaign.active_version_id
    campaign.fired_at = _now()
    await session.flush()

    # Ensure exactly one stats row (audience_size filled by the snapshot).
    await session.execute(
        pg_insert(CampaignStats)
        .values(id=uuid7(), workspace_id=principal.workspace_id, campaign_id=campaign.id)
        .on_conflict_do_nothing(
            index_elements=[CampaignStats.workspace_id, CampaignStats.campaign_id]
        )
    )

    await outbox.emit(
        session,
        aggregate=events.AGGREGATE_CAMPAIGN,
        aggregate_id=campaign.id,
        topic=events.CAMPAIGN_FIRED,
        payload={
            "workspace_id": encode_public_id(IdPrefix.WORKSPACE, principal.workspace_id),
            "campaign_id": encode_public_id(IdPrefix.CAMPAIGN, campaign.id),
            "version_id": encode_public_id(IdPrefix.CAMPAIGN_VERSION, campaign.fired_version_id),
        },
    )
    return campaign_out(campaign)


# --- Snapshot + chunked enqueue (worker path) --------------------------------------------------


def _message_id(campaign_id: uuid.UUID, contact_id: uuid.UUID) -> str:
    """Deterministic RFC-822 Message-ID (stable across retries → MTA dedupes crash re-sends)."""
    return f"<cpn-{campaign_id.hex}-{contact_id.hex}@{get_settings().email_inbound_domain}>"


async def claim_snapshot(
    workspace_id: uuid.UUID, campaign_id: uuid.UUID
) -> tuple[uuid.UUID, dict[str, Any]] | None:
    """Return ``(version_id, segment)`` to snapshot, or ``None`` if already snapshotted/ineligible.

    Not a hard latch (snapshot_done_at is set at the end): a duplicate fire is made harmless by the
    ``sends`` ON CONFLICT pre-insert + the per-send claim, so a re-run only resumes/repeats no-ops.
    """
    async with session_scope(workspace_id) as session:
        campaign = await session.get(Campaign, campaign_id)
        if campaign is None or campaign.snapshot_done_at is not None:
            return None
        version_id = campaign.fired_version_id or campaign.active_version_id
        if version_id is None:
            return None
        return version_id, dict(campaign.segment or {})


async def preinsert_sends(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    campaign_id: uuid.UUID,
    version_id: uuid.UUID,
    members: list[tuple[uuid.UUID, str | None]],
) -> None:
    """Durably materialise the audience as ``queued`` send rows (idempotent via the claim-slot
    unique). This is the frozen snapshot — compliance is re-checked per row at send time."""
    if not members:
        return
    rows = [
        {
            "id": uuid7(),
            "workspace_id": workspace_id,
            "campaign_id": campaign_id,
            "campaign_version_id": version_id,
            "contact_id": contact_id,
            "email": email or "",
            "message_id": _message_id(campaign_id, contact_id),
            "status": "queued",
        }
        for contact_id, email in members
    ]
    await session.execute(
        pg_insert(Send)
        .values(rows)
        .on_conflict_do_nothing(
            index_elements=[Send.workspace_id, Send.campaign_id, Send.contact_id]
        )
    )


async def finalize_snapshot(
    workspace_id: uuid.UUID, campaign_id: uuid.UUID, audience_size: int
) -> None:
    async with session_scope(workspace_id) as session:
        await session.execute(
            update(CampaignStats)
            .where(CampaignStats.campaign_id == campaign_id)
            .values(audience_size=audience_size, updated_at=_now())
        )
        campaign = await session.get(Campaign, campaign_id)
        if campaign is not None:
            campaign.snapshot_done_at = _now()
            if audience_size == 0:
                campaign.status = "sent"


async def run_fire_snapshot(
    workspace_id: uuid.UUID,
    campaign_id: uuid.UUID,
    *,
    enqueue: Callable[[list[uuid.UUID]], None],
    batch_size: int | None = None,
) -> str:
    """Snapshot the audience on the replica, pre-insert ``queued`` sends, and enqueue send chunks.

    ``enqueue`` publishes a ``send_chunk`` task for a batch of contact ids (kept out of the service
    so the Celery dependency stays in tasks.py). Resumable: re-running repeats idempotent inserts.
    """
    claim = await claim_snapshot(workspace_id, campaign_id)
    if claim is None:
        return "already_snapshotted"
    version_id, segment = claim
    size = batch_size or get_settings().outbound_chunk_size
    total = 0
    async for batch in crm_service.snapshot_audience(workspace_id, segment, batch_size=size):
        async with session_scope(workspace_id) as session:
            await preinsert_sends(
                session,
                workspace_id=workspace_id,
                campaign_id=campaign_id,
                version_id=version_id,
                members=batch,
            )
        enqueue([contact_id for contact_id, _email in batch])
        total += len(batch)
    await finalize_snapshot(workspace_id, campaign_id, total)
    return "snapshotted"


# --- Per-recipient send (the exactly-once core) ------------------------------------------------


async def _emit_send(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    campaign_id: uuid.UUID,
    contact_id: uuid.UUID,
    topic: str,
    extra: dict[str, Any] | None = None,
) -> None:
    await outbox.emit(
        session,
        aggregate=events.AGGREGATE_CAMPAIGN,
        aggregate_id=campaign_id,
        topic=topic,
        payload={
            "workspace_id": encode_public_id(IdPrefix.WORKSPACE, workspace_id),
            "campaign_id": encode_public_id(IdPrefix.CAMPAIGN, campaign_id),
            "contact_id": encode_public_id(IdPrefix.CONTACT, contact_id),
            "occurred_at": _now().isoformat(),
            **(extra or {}),
        },
    )


async def send_one(
    *, workspace_id: uuid.UUID, campaign_id: uuid.UUID, contact_id: uuid.UUID
) -> str:
    """Send one campaign email to one contact — exactly-once, compliance-gated at send time.

    Claims the ``queued`` send row (atomic conditional UPDATE); a second worker or a re-fire finds
    no ``queued`` row and returns. Compliance (suppression → consent → frequency) is re-checked live
    here (never from the snapshot). A transient rate-limit rolls the whole txn back so the row
    returns to ``queued`` and the retry re-claims cleanly.
    """
    settings = get_settings()
    async with session_scope(workspace_id) as session:
        campaign = await session.get(Campaign, campaign_id)
        if campaign is None:
            return "campaign_gone"
        if campaign.status in ("paused", "cancelled"):
            return "paused"  # leave the row queued for resume

        claimed = (
            await session.execute(
                update(Send)
                .where(
                    Send.workspace_id == workspace_id,
                    Send.campaign_id == campaign_id,
                    Send.contact_id == contact_id,
                    Send.status == "queued",
                )
                .values(status="sending")
                .returning(Send.id, Send.email, Send.message_id, Send.campaign_version_id)
            )
        ).first()
        if claimed is None:
            return "already_processed"
        send_id, email, message_id, version_id = claimed

        subtype = (
            await session.get(SubscriptionType, campaign.subscription_type_id)
            if campaign.subscription_type_id
            else None
        )

        # Send-time gates (compliance re-checked live — P1.8 acceptance #2/#4).
        skip_reason: str | None = None
        if not email:
            skip_reason = "no_email"
        elif not await crm_service.contact_is_active(session, contact_id):
            # Soft-deleted / GDPR-erased after the snapshot — never mail a deleted contact.
            skip_reason = "contact_deleted"
        elif await channels_service.is_suppressed(session, workspace_id, email):
            skip_reason = "suppressed"
        elif subtype is not None and await is_blocked_by_consent(
            session, contact_id=contact_id, subscription_type=subtype
        ):
            skip_reason = "unsubscribed"
        elif await ratelimit.frequency_exceeded(workspace_id, contact_id):
            skip_reason = "freq_capped"

        if skip_reason is not None:
            await session.execute(
                update(Send)
                .where(Send.id == send_id)
                .values(status="skipped", skip_reason=skip_reason)
            )
            await _emit_send(
                session,
                workspace_id=workspace_id,
                campaign_id=campaign_id,
                contact_id=contact_id,
                topic=events.CAMPAIGN_SEND_SKIPPED,
                extra={"skip_reason": skip_reason},
            )
            return f"skipped:{skip_reason}"

        # Rate limit AFTER the gates: a transient denial rolls back to 'queued' for a clean retry.
        if not await ratelimit.acquire_send_tokens(workspace_id):
            raise RateLimitedError("outbound send rate exceeded")

        version = await session.get(CampaignVersion, version_id)
        if version is None:
            await session.execute(
                update(Send).where(Send.id == send_id).values(status="failed", error="version_gone")
            )
            await _emit_send(
                session,
                workspace_id=workspace_id,
                campaign_id=campaign_id,
                contact_id=contact_id,
                topic=events.CAMPAIGN_SEND_FAILED,
                extra={"error": "version_gone"},
            )
            return "failed"

        _resolved_email, name = await crm_service.contact_email(session, contact_id)
        context: dict[str, Any] = {
            "contact": {"email": email, "name": name},
            **(version.variables or {}),
        }
        rendered = mjml.render_email(template=version.mjml, context=context)
        # Subject is a plain-text header — don't HTML-escape (would corrupt legitimate &/<//>).
        subject = mjml.substitute(version.subject, context, escape=False)

        list_unsubscribe_url: str | None = None
        if subtype is not None and subtype.kind == "marketing":
            token = make_unsubscribe_token(workspace_id, contact_id, subtype.id)
            list_unsubscribe_url = f"{settings.public_api_base_url}/v0/outbound/u/{token}"

        from_addr = f"noreply@{settings.email_inbound_domain}"
        from_name = version.from_name or settings.email_from_name
        try:
            provider_id = await channels_service.send_broadcast_email(
                from_addr=from_addr,
                from_name=from_name,
                to_addr=email,
                subject=subject,
                html_body=rendered.html,
                text_body=rendered.text,
                message_id=message_id,
                reply_to=version.reply_to,
                list_unsubscribe_url=list_unsubscribe_url,
            )
        except channels_service.MessageTooLarge:
            await session.execute(
                update(Send).where(Send.id == send_id).values(status="failed", error="too_large")
            )
            await _emit_send(
                session,
                workspace_id=workspace_id,
                campaign_id=campaign_id,
                contact_id=contact_id,
                topic=events.CAMPAIGN_SEND_FAILED,
                extra={"error": "too_large"},
            )
            return "failed:too_large"

        await session.execute(
            update(Send)
            .where(Send.id == send_id)
            .values(status="sent", provider_id=provider_id, sent_at=_now())
        )
        await _emit_send(
            session,
            workspace_id=workspace_id,
            campaign_id=campaign_id,
            contact_id=contact_id,
            topic=events.CAMPAIGN_SEND_SENT,
            extra={"provider_id": provider_id},
        )

    # Frequency counter advances only on a committed successful send (best-effort v0).
    if subtype is not None and subtype.kind == "marketing":
        await ratelimit.record_frequency(workspace_id, contact_id)
    return "sent"


# --- Provider engagement events → message_events + stats ---------------------------------------

_EVENT_TO_TOPIC: dict[str, str] = {
    "delivered": events.CAMPAIGN_EVENT_DELIVERED,
    "open": events.CAMPAIGN_EVENT_OPEN,
    "click": events.CAMPAIGN_EVENT_CLICK,
    "bounce": events.CAMPAIGN_EVENT_BOUNCE,
    "complaint": events.CAMPAIGN_EVENT_COMPLAINT,
    "unsub": events.CAMPAIGN_EVENT_UNSUB,
}


async def record_engagement_event(
    *,
    workspace_id: uuid.UUID,
    provider_message_id: str,
    event_kind: str,
    sns_message_id: str | None = None,
    occurred_at: dt.datetime | None = None,
    detail: dict[str, Any] | None = None,
) -> str:
    """Record a provider engagement event (delivered/open/click/bounce/complaint/unsub) for a send.

    Writes the append-only ``message_events`` audit row (every event) and, on the **first**
    occurrence of that kind for the (campaign, contact), emits the stats event — so opens/clicks are
    counted unique-per-contact (P1.8 report semantics). Bounce/complaint additionally suppress the
    address (via channels) so it is never mailed again. Resolves the send by provider message id
    within the given workspace; returns ``unresolved`` if no matching send exists.

    The SNS-delivery dedupe (``sns_message_id``) is claimed **inside this same transaction** as the
    write, so a retry after a mid-record failure re-processes the event rather than dropping it (the
    dedupe row only persists if the recording commits).
    """
    if event_kind not in _EVENT_TO_TOPIC:
        raise ValidationError(f"unknown engagement event {event_kind!r}")
    async with session_scope(workspace_id) as session:
        if sns_message_id is not None:
            claimed = (
                await session.execute(
                    pg_insert(OutboundEventDedupe)
                    .values(sns_message_id=sns_message_id)
                    .on_conflict_do_nothing(index_elements=[OutboundEventDedupe.sns_message_id])
                    .returning(OutboundEventDedupe.sns_message_id)
                )
            ).first()
            if claimed is None:
                return "duplicate_sns"

        send = await session.scalar(
            select(Send).where(
                Send.workspace_id == workspace_id, Send.provider_id == provider_message_id
            )
        )
        if send is None:
            return "unresolved"

        already = await session.scalar(
            select(MessageEvent.id).where(
                MessageEvent.workspace_id == workspace_id,
                MessageEvent.campaign_id == send.campaign_id,
                MessageEvent.contact_id == send.contact_id,
                MessageEvent.event == event_kind,
            )
        )
        session.add(
            MessageEvent(
                id=uuid7(),
                workspace_id=workspace_id,
                source_kind="email",
                source_id=send.campaign_id,
                campaign_id=send.campaign_id,
                contact_id=send.contact_id,
                email=send.email,
                event=event_kind,
                provider_id=provider_message_id,
                provider_event_id=sns_message_id,
                detail=detail or {},
                created_at=occurred_at or _now(),
            )
        )
        await session.flush()

        if already is None:  # first of this kind for the contact → count it
            await _emit_send(
                session,
                workspace_id=workspace_id,
                campaign_id=send.campaign_id,
                contact_id=send.contact_id,
                topic=_EVENT_TO_TOPIC[event_kind],
            )

        if event_kind in ("bounce", "complaint") and send.email:
            await channels_service.suppress_address(
                session,
                workspace_id=workspace_id,
                email=send.email,
                reason=event_kind,
                source="ses",
                detail=detail or {},
            )
        return "recorded" if already is None else "duplicate"


async def reconcile_campaign_stats(workspace_id: uuid.UUID, campaign_id: uuid.UUID) -> None:
    """Recompute ``campaign_stats`` counters from the source-of-truth ledgers (the ±0.5% safety net
    behind the streamed projection). Sends come from the ``sends`` ledger; engagement is counted
    unique-per-contact from ``message_events``.
    """
    async with session_scope(workspace_id) as session:
        send_counts: dict[str, int] = dict(
            (
                await session.execute(
                    select(Send.status, func.count())
                    .where(Send.campaign_id == campaign_id)
                    .group_by(Send.status)
                )
            )
            .tuples()
            .all()
        )
        engagement: dict[str, int] = dict(
            (
                await session.execute(
                    select(MessageEvent.event, func.count(func.distinct(MessageEvent.contact_id)))
                    .where(MessageEvent.campaign_id == campaign_id)
                    .group_by(MessageEvent.event)
                )
            )
            .tuples()
            .all()
        )
        await session.execute(
            update(CampaignStats)
            .where(CampaignStats.campaign_id == campaign_id)
            .values(
                sent=int(send_counts.get("sent", 0)),
                skipped=int(send_counts.get("skipped", 0)),
                failed=int(send_counts.get("failed", 0)),
                delivered=int(engagement.get("delivered", 0)),
                opened=int(engagement.get("open", 0)),
                clicked=int(engagement.get("click", 0)),
                bounced=int(engagement.get("bounce", 0)),
                complained=int(engagement.get("complaint", 0)),
                unsubscribed=int(engagement.get("unsub", 0)),
                updated_at=_now(),
            )
        )


# --- SES engagement webhook ingestion (pre-tenancy) --------------------------------------------

_SES_EVENT_MAP: dict[str, str] = {
    "Delivery": "delivered",
    "Open": "open",
    "Click": "click",
    "Bounce": "bounce",
    "Complaint": "complaint",
}


async def resolve_send_workspace(provider_message_id: str) -> uuid.UUID | None:
    """Map an SES MessageId to its workspace via the SECURITY DEFINER resolver (pre-tenancy)."""
    async with session_scope(None) as session:
        row = (
            await session.execute(
                text("SELECT workspace_id FROM relay_outbound_resolve_send(:p)"),
                {"p": provider_message_id},
            )
        ).first()
    return uuid.UUID(str(row[0])) if row is not None else None


async def ingest_ses_engagement(
    *,
    provider_message_id: str,
    ses_event_type: str,
    sns_message_id: str | None = None,
    occurred_at: dt.datetime | None = None,
    detail: dict[str, Any] | None = None,
) -> str:
    """Ingest one SES engagement notification (config-set event) for a campaign send.

    Resolves the owning workspace by SES MessageId, then records the engagement (message_events +
    stats event; bounce/complaint also suppress). The SNS-delivery dedupe is claimed *inside*
    ``record_engagement_event``'s transaction so a mid-record failure + retry re-processes the event
    rather than dropping it. Non-SES or non-campaign messages return ``ignored``/``unresolved`` and
    are no-ops (an agent-reply bounce, which channels handles separately, does not error here).
    """
    kind = _SES_EVENT_MAP.get(ses_event_type)
    if kind is None:
        return "ignored"
    workspace_id = await resolve_send_workspace(provider_message_id)
    if workspace_id is None:
        return "unresolved"
    return await record_engagement_event(
        workspace_id=workspace_id,
        provider_message_id=provider_message_id,
        event_kind=kind,
        sns_message_id=sns_message_id,
        occurred_at=occurred_at,
        detail=detail,
    )


# --- In-app posts & chats ----------------------------------------------------------------------


def post_out(p: Post) -> schemas.PostOut:
    return schemas.PostOut(
        id=encode_public_id(IdPrefix.POST, p.id),
        kind=p.kind,
        title=p.title,
        body=p.body,
        status=p.status,
        subscription_type_id=(
            encode_public_id(IdPrefix.SUBSCRIPTION_TYPE, p.subscription_type_id)
            if p.subscription_type_id
            else None
        ),
        segment=p.segment,
        audience_size=p.audience_size,
        fired_at=p.fired_at,
        created_at=p.created_at,
    )


async def _get_post(session: AsyncSession, post_id: uuid.UUID) -> Post:
    row = await session.get(Post, post_id)
    if row is None:
        raise NotFoundError("post not found")
    return row


def _post_body_text(post: Post) -> str:
    """Best-effort plain-text for a chat's first message (posts render their own rich body)."""
    body = post.body or {}
    text_val = body.get("text") if isinstance(body, dict) else None
    return str(text_val or post.title or "")


async def create_post(
    session: AsyncSession, principal: Principal, req: schemas.PostCreate
) -> schemas.PostOut:
    authorize(principal, min_role=Role.ADMIN)
    await crm_service.validate_contact_audience(session, req.segment)
    subscription_type_id: uuid.UUID | None = None
    if req.subscription_type_id is not None:
        subscription_type_id = _decode_or_404(
            IdPrefix.SUBSCRIPTION_TYPE, req.subscription_type_id, "subscription type"
        )
        await _get_subscription_type(session, subscription_type_id)
    post = Post(
        workspace_id=principal.workspace_id,
        kind=req.kind,
        title=req.title,
        body=req.body,
        segment=req.segment,
        subscription_type_id=subscription_type_id,
        created_by=principal.admin_id,
    )
    session.add(post)
    await session.flush()
    return post_out(post)


async def list_posts(session: AsyncSession) -> list[schemas.PostOut]:
    rows = (await session.scalars(select(Post).order_by(Post.created_at.desc()))).all()
    return [post_out(r) for r in rows]


async def get_post(session: AsyncSession, public_id: str) -> schemas.PostOut:
    return post_out(await _get_post(session, _decode_or_404(IdPrefix.POST, public_id, "post")))


async def fire_post(session: AsyncSession, principal: Principal, public_id: str) -> schemas.PostOut:
    authorize(principal, min_role=Role.ADMIN)
    post = await _get_post(session, _decode_or_404(IdPrefix.POST, public_id, "post"))
    if post.status not in _FIREABLE:
        raise ConflictError(f"post is not in a fireable state (status={post.status})")
    post.status = "firing"
    post.fired_at = _now()
    await session.flush()
    await outbox.emit(
        session,
        aggregate=events.AGGREGATE_POST,
        aggregate_id=post.id,
        topic=events.POST_FIRED,
        payload={
            "workspace_id": encode_public_id(IdPrefix.WORKSPACE, principal.workspace_id),
            "post_id": encode_public_id(IdPrefix.POST, post.id),
        },
    )
    return post_out(post)


async def claim_post_snapshot(workspace_id: uuid.UUID, post_id: uuid.UUID) -> dict[str, Any] | None:
    async with session_scope(workspace_id) as session:
        post = await session.get(Post, post_id)
        if post is None or post.snapshot_done_at is not None:
            return None
        return dict(post.segment or {})


async def preinsert_post_receipts(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    post_id: uuid.UUID,
    members: list[tuple[uuid.UUID, str | None]],
) -> None:
    if not members:
        return
    rows = [
        {
            "id": uuid7(),
            "workspace_id": workspace_id,
            "post_id": post_id,
            "contact_id": contact_id,
            "state": "pending",
        }
        for contact_id, _email in members
    ]
    await session.execute(
        pg_insert(PostReceipt)
        .values(rows)
        .on_conflict_do_nothing(
            index_elements=[PostReceipt.workspace_id, PostReceipt.post_id, PostReceipt.contact_id]
        )
    )


async def run_post_snapshot(
    workspace_id: uuid.UUID,
    post_id: uuid.UUID,
    *,
    enqueue: Callable[[list[uuid.UUID]], None],
    batch_size: int | None = None,
) -> str:
    """Snapshot the audience into ``pending`` post_receipts and enqueue delivery chunks."""
    claim = await claim_post_snapshot(workspace_id, post_id)
    if claim is None:
        return "already_snapshotted"
    segment = claim
    size = batch_size or get_settings().outbound_chunk_size
    total = 0
    async for batch in crm_service.snapshot_audience(workspace_id, segment, batch_size=size):
        async with session_scope(workspace_id) as session:
            await preinsert_post_receipts(
                session, workspace_id=workspace_id, post_id=post_id, members=batch
            )
        enqueue([contact_id for contact_id, _email in batch])
        total += len(batch)
    async with session_scope(workspace_id) as session:
        post = await session.get(Post, post_id)
        if post is not None:
            post.audience_size = total
            post.snapshot_done_at = _now()
            if total == 0:
                post.status = "sent"
    return "snapshotted"


async def deliver_post_receipt(
    *, workspace_id: uuid.UUID, post_id: uuid.UUID, contact_id: uuid.UUID
) -> str:
    """Deliver one in-app post/chat to one contact — exactly-once via the receipt claim slot.

    Consent (for a marketing subscription type) is re-checked here at delivery time. A ``post``
    emits ``outbound.post.delivered`` (fanned out to the contact's channel + caught up on next
    widget boot); a ``chat`` opens an outbound conversation the contact can reply to.
    """
    async with session_scope(workspace_id) as session:
        post = await session.get(Post, post_id)
        if post is None:
            return "post_gone"
        if post.status in ("paused", "cancelled"):
            return "paused"
        receipt = (
            await session.execute(
                select(PostReceipt)
                .where(
                    PostReceipt.workspace_id == workspace_id,
                    PostReceipt.post_id == post_id,
                    PostReceipt.contact_id == contact_id,
                    PostReceipt.state == "pending",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if receipt is None:
            return "already_processed"

        subtype = (
            await session.get(SubscriptionType, post.subscription_type_id)
            if post.subscription_type_id
            else None
        )
        if not await crm_service.contact_is_active(session, contact_id):
            # Soft-deleted / erased after snapshot — terminal skip (also avoids a chat FK error).
            receipt.state = "skipped"
            receipt.skip_reason = "contact_deleted"
            return "skipped:contact_deleted"

        if subtype is not None and await is_blocked_by_consent(
            session, contact_id=contact_id, subscription_type=subtype
        ):
            receipt.state = "suppressed_consent"
            receipt.skip_reason = "unsubscribed"
            return "skipped:unsubscribed"

        if post.kind == "chat":
            author_kind = "admin" if post.created_by is not None else "system"
            receipt.conversation_id = await messaging_service.create_outbound_conversation(
                session,
                workspace_id=workspace_id,
                contact_id=contact_id,
                body=_post_body_text(post),
                author_kind=author_kind,
                author_id=post.created_by,
            )

        receipt.state = "delivered"
        receipt.delivered_at = _now()
        session.add(
            MessageEvent(
                id=uuid7(),
                workspace_id=workspace_id,
                source_kind=post.kind,
                source_id=post_id,
                contact_id=contact_id,
                event="delivered",
                created_at=_now(),
            )
        )
        await outbox.emit(
            session,
            aggregate=events.AGGREGATE_POST,
            aggregate_id=post_id,
            topic=events.POST_DELIVERED,
            payload={
                "workspace_id": encode_public_id(IdPrefix.WORKSPACE, workspace_id),
                "contact_id": encode_public_id(IdPrefix.CONTACT, contact_id),
                "post_id": encode_public_id(IdPrefix.POST, post_id),
                "kind": post.kind,
                "title": post.title,
                "occurred_at": _now().isoformat(),
            },
        )
        return "delivered"


async def pending_posts_for_contact(
    session: AsyncSession, contact_id: uuid.UUID, *, limit: int = 20
) -> list[dict[str, Any]]:
    """Delivered-but-unseen feed posts for a contact — the widget-boot catch-up payload (P1.8)."""
    rows = (
        await session.execute(
            select(Post, PostReceipt)
            .join(PostReceipt, PostReceipt.post_id == Post.id)
            .where(
                PostReceipt.contact_id == contact_id,
                PostReceipt.state == "delivered",
                PostReceipt.seen_at.is_(None),
                Post.kind == "post",
            )
            .order_by(PostReceipt.delivered_at.desc())
            .limit(limit)
        )
    ).all()
    return [
        {
            "id": encode_public_id(IdPrefix.POST, post.id),
            "receipt_id": encode_public_id(IdPrefix.POST_RECEIPT, receipt.id),
            "kind": post.kind,
            "title": post.title,
            "body": post.body,
            "delivered_at": receipt.delivered_at.isoformat() if receipt.delivered_at else None,
        }
        for post, receipt in rows
    ]


async def _mark_post_engagement(
    session: AsyncSession, *, contact_id: uuid.UUID, receipt_public_id: str, event: str
) -> None:
    receipt_id = _decode_or_404(IdPrefix.POST_RECEIPT, receipt_public_id, "post receipt")
    receipt = await session.get(PostReceipt, receipt_id)
    if receipt is None or receipt.contact_id != contact_id:
        raise NotFoundError("post receipt not found")
    source_kind = (
        await session.scalar(select(Post.kind).where(Post.id == receipt.post_id)) or "post"
    )
    now = _now()
    if event == "seen":
        if receipt.seen_at is None:
            receipt.seen_at = now
            if receipt.state == "delivered":
                receipt.state = "seen"
    elif event == "click":
        receipt.clicked_at = receipt.clicked_at or now
        if receipt.state in ("delivered", "seen"):
            receipt.state = "clicked"
    session.add(
        MessageEvent(
            id=uuid7(),
            workspace_id=receipt.workspace_id,
            source_kind=source_kind,
            source_id=receipt.post_id,
            contact_id=contact_id,
            event=event,
            created_at=now,
        )
    )


async def mark_post_seen(
    session: AsyncSession, contact_id: uuid.UUID, receipt_public_id: str
) -> None:
    await _mark_post_engagement(
        session, contact_id=contact_id, receipt_public_id=receipt_public_id, event="seen"
    )


async def mark_post_clicked(
    session: AsyncSession, contact_id: uuid.UUID, receipt_public_id: str
) -> None:
    await _mark_post_engagement(
        session, contact_id=contact_id, receipt_public_id=receipt_public_id, event="click"
    )
