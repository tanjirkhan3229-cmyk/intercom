"""Service layer for the ``webhooks`` module (P0.11).

Manages webhook subscriptions (CRUD + secret rotation) and the delivery log (list/get/redeliver).
All mutations are admin-gated through the one RBAC choke point (``rbac.authorize``); managing
integrations is an admin action, and these routes are never in the API-key allowlist, so a public
key can't reach them. Signing secrets are encrypted at rest (core/crypto) and returned in plaintext
exactly once (on create + rotate), mirroring how ``api_keys`` returns its key.

Redelivery does not enqueue inline (a request handler can't safely enqueue before its transaction
commits): it inserts a fresh ``pending`` delivery row that is *due now*, which the durable
``webhooks.scan_retries`` beat task picks up — commit-safe and reusing the retry path.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import functools
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from relay.core.crypto import encrypt_secret
from relay.core.errors import NotFoundError
from relay.core.ids import IdPrefix, decode_public_id, encode_public_id, uuid7
from relay.core.pagination import Page, clamp_limit
from relay.core.principal import Principal
from relay.core.rbac import Role, authorize
from relay.core.security import generate_secret
from relay.core.ssrf import validate_target
from relay.settings import get_settings

from . import schemas
from .models import WebhookDelivery, WebhookSubscription


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def _validate_target_async(url: str, *, allow_private: bool) -> None:
    """Run the SSRF check off the event loop — ``validate_target`` does a blocking ``getaddrinfo``
    and these handlers are native ``async def`` (master rule #5: no blocking calls in async paths).
    Raises ``SsrfError`` (422) for a disallowed target, exactly as the sync call would."""
    await asyncio.get_running_loop().run_in_executor(
        None, functools.partial(validate_target, url, allow_private=allow_private)
    )


def _decode_or_404(prefix: str, public_id: str, what: str) -> uuid.UUID:
    try:
        return decode_public_id(prefix, public_id)
    except ValueError as exc:
        raise NotFoundError(f"{what} not found") from exc


def _sub_out(s: WebhookSubscription) -> schemas.WebhookSubscriptionOut:
    return schemas.WebhookSubscriptionOut(
        id=encode_public_id(IdPrefix.WEBHOOK_SUBSCRIPTION, s.id),
        url=s.url,
        topics=list(s.topics),
        status=s.status,
        secret_last4=s.secret_last4,
        consecutive_failures=s.consecutive_failures,
        last_error=s.last_error,
        last_success_at=s.last_success_at,
        disabled_at=s.disabled_at,
        created_at=s.created_at,
    )


def _delivery_out(d: WebhookDelivery) -> schemas.WebhookDeliveryOut:
    return schemas.WebhookDeliveryOut(
        id=encode_public_id(IdPrefix.WEBHOOK_DELIVERY, d.id),
        subscription_id=encode_public_id(IdPrefix.WEBHOOK_SUBSCRIPTION, d.subscription_id),
        topic=d.topic,
        status=d.status,
        attempt=d.attempt,
        response_code=d.response_code,
        error=d.error,
        next_attempt_at=d.next_attempt_at,
        delivered_at=d.delivered_at,
        created_at=d.created_at,
    )


async def _load_sub(session: AsyncSession, sub_id: uuid.UUID) -> WebhookSubscription:
    # RLS scopes the lookup to the caller's workspace, so a foreign id reads as not-found.
    sub = await session.get(WebhookSubscription, sub_id)
    if sub is None:
        raise NotFoundError("webhook subscription not found")
    return sub


async def _load_delivery(
    session: AsyncSession, sub_id: uuid.UUID, delivery_id: uuid.UUID
) -> WebhookDelivery:
    d = (
        await session.scalars(
            select(WebhookDelivery).where(
                WebhookDelivery.subscription_id == sub_id, WebhookDelivery.id == delivery_id
            )
        )
    ).one_or_none()
    if d is None:
        raise NotFoundError("webhook delivery not found")
    return d


async def create_subscription(
    session: AsyncSession, principal: Principal, req: schemas.WebhookSubscriptionCreate
) -> schemas.WebhookSubscriptionCreated:
    authorize(principal, min_role=Role.ADMIN)
    allow_private = get_settings().webhook_allow_private_targets
    await _validate_target_async(req.url, allow_private=allow_private)  # SSRF check (raises 422)
    secret = generate_secret()
    sub = WebhookSubscription(
        workspace_id=principal.workspace_id,
        url=req.url,
        secret_ciphertext=encrypt_secret(secret),
        secret_last4=secret[-4:],
        topics=list(req.topics),
        created_by=principal.admin_id,
    )
    session.add(sub)
    await session.flush()
    out = _sub_out(sub)
    return schemas.WebhookSubscriptionCreated(**out.model_dump(), secret=secret)


async def list_subscriptions(
    session: AsyncSession, *, cursor: str | None = None, limit: int | None = None
) -> Page[schemas.WebhookSubscriptionOut]:
    n = clamp_limit(limit)
    stmt = select(WebhookSubscription).order_by(WebhookSubscription.id.desc())
    if cursor:
        cid = _decode_or_404(IdPrefix.WEBHOOK_SUBSCRIPTION, cursor, "cursor")
        stmt = stmt.where(WebhookSubscription.id < cid)
    rows = list((await session.scalars(stmt.limit(n + 1))).all())
    next_cursor = None
    if len(rows) > n:
        rows = rows[:n]
        next_cursor = encode_public_id(IdPrefix.WEBHOOK_SUBSCRIPTION, rows[-1].id)
    return Page(items=[_sub_out(s) for s in rows], next_cursor=next_cursor)


async def get_subscription(
    session: AsyncSession, sub_public_id: str
) -> schemas.WebhookSubscriptionOut:
    sub = await _load_sub(
        session,
        _decode_or_404(IdPrefix.WEBHOOK_SUBSCRIPTION, sub_public_id, "webhook subscription"),
    )
    return _sub_out(sub)


async def update_subscription(
    session: AsyncSession,
    principal: Principal,
    sub_public_id: str,
    req: schemas.WebhookSubscriptionUpdate,
) -> schemas.WebhookSubscriptionOut:
    authorize(principal, min_role=Role.ADMIN)
    sub = await _load_sub(
        session,
        _decode_or_404(IdPrefix.WEBHOOK_SUBSCRIPTION, sub_public_id, "webhook subscription"),
    )
    if req.url is not None and req.url != sub.url:
        await _validate_target_async(
            req.url, allow_private=get_settings().webhook_allow_private_targets
        )
        sub.url = req.url
    if req.topics is not None:
        sub.topics = list(req.topics)
    if req.status is not None:
        sub.status = req.status
        if req.status == "active":
            # Re-enabling clears the failure state so the breaker/auto-disable start fresh.
            sub.consecutive_failures = 0
            sub.disabled_at = None
            sub.last_error = None
    sub.updated_at = _now()
    await session.flush()
    return _sub_out(sub)


async def delete_subscription(
    session: AsyncSession, principal: Principal, sub_public_id: str
) -> None:
    authorize(principal, min_role=Role.ADMIN)
    sub = await _load_sub(
        session,
        _decode_or_404(IdPrefix.WEBHOOK_SUBSCRIPTION, sub_public_id, "webhook subscription"),
    )
    await session.delete(sub)
    await session.flush()


async def rotate_secret(
    session: AsyncSession, principal: Principal, sub_public_id: str
) -> schemas.WebhookSubscriptionCreated:
    authorize(principal, min_role=Role.ADMIN)
    sub = await _load_sub(
        session,
        _decode_or_404(IdPrefix.WEBHOOK_SUBSCRIPTION, sub_public_id, "webhook subscription"),
    )
    secret = generate_secret()
    sub.secret_ciphertext = encrypt_secret(secret)
    sub.secret_last4 = secret[-4:]
    sub.updated_at = _now()
    await session.flush()
    out = _sub_out(sub)
    return schemas.WebhookSubscriptionCreated(**out.model_dump(), secret=secret)


async def list_deliveries(
    session: AsyncSession,
    sub_public_id: str,
    *,
    cursor: str | None = None,
    limit: int | None = None,
) -> Page[schemas.WebhookDeliveryOut]:
    sub_id = _decode_or_404(IdPrefix.WEBHOOK_SUBSCRIPTION, sub_public_id, "webhook subscription")
    await _load_sub(session, sub_id)  # 404 (or cross-tenant) if the subscription isn't ours
    n = clamp_limit(limit)
    stmt = (
        select(WebhookDelivery)
        .where(WebhookDelivery.subscription_id == sub_id)
        .order_by(WebhookDelivery.id.desc())
    )
    if cursor:
        cid = _decode_or_404(IdPrefix.WEBHOOK_DELIVERY, cursor, "cursor")
        stmt = stmt.where(WebhookDelivery.id < cid)
    rows = list((await session.scalars(stmt.limit(n + 1))).all())
    next_cursor = None
    if len(rows) > n:
        rows = rows[:n]
        next_cursor = encode_public_id(IdPrefix.WEBHOOK_DELIVERY, rows[-1].id)
    return Page(items=[_delivery_out(d) for d in rows], next_cursor=next_cursor)


async def get_delivery(
    session: AsyncSession, sub_public_id: str, delivery_public_id: str
) -> schemas.WebhookDeliveryOut:
    sub_id = _decode_or_404(IdPrefix.WEBHOOK_SUBSCRIPTION, sub_public_id, "webhook subscription")
    delivery_id = _decode_or_404(IdPrefix.WEBHOOK_DELIVERY, delivery_public_id, "webhook delivery")
    return _delivery_out(await _load_delivery(session, sub_id, delivery_id))


async def redeliver(
    session: AsyncSession, principal: Principal, sub_public_id: str, delivery_public_id: str
) -> schemas.WebhookDeliveryOut:
    authorize(principal, min_role=Role.ADMIN)
    sub_id = _decode_or_404(IdPrefix.WEBHOOK_SUBSCRIPTION, sub_public_id, "webhook subscription")
    await _load_sub(session, sub_id)
    delivery_id = _decode_or_404(IdPrefix.WEBHOOK_DELIVERY, delivery_public_id, "webhook delivery")
    src = await _load_delivery(session, sub_id, delivery_id)
    now = _now()
    # A fresh row that is due now → picked up by the durable scan_retries beat task (commit-safe,
    # no inline enqueue). A different created_at from the source keeps the unique key distinct.
    fresh = WebhookDelivery(
        id=uuid7(),
        workspace_id=principal.workspace_id,
        subscription_id=sub_id,
        outbox_id=src.outbox_id,
        topic=src.topic,
        payload=src.payload,
        attempt=0,
        status="pending",
        next_attempt_at=now,
        created_at=now,
    )
    session.add(fresh)
    await session.flush()
    return _delivery_out(fresh)
