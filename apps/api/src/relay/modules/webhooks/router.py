"""HTTP routes for the ``webhooks`` module (P0.11), mounted under ``/v0``.

Subscription management + delivery-log inspection + manual redelivery. All are admin-only (the
service layer enforces it) and JWT-authenticated — these routes are deliberately *not* in the
public-API key allowlist, so an API key cannot manage webhooks.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Response, status

from relay.core.deps import CurrentPrincipal, SessionDep
from relay.core.pagination import Page

from . import schemas, service

router = APIRouter(tags=["webhooks"])


# NOT @idempotent: the 201 response carries the one-time plaintext signing secret, and the
# idempotency store would persist that whole response as cleartext in ``idempotency_keys`` — a
# confidentiality leak that defeats the Fernet-at-rest design. Subscription management is an
# admin/JWT action (not the public API-key surface), so master rule 3 does not require it; this
# matches create_api_key + rotate_secret, which are likewise not idempotent for the same reason.
@router.post(
    "/webhook_subscriptions",
    response_model=schemas.WebhookSubscriptionCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_subscription(
    req: schemas.WebhookSubscriptionCreate,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> schemas.WebhookSubscriptionCreated:
    return await service.create_subscription(session, principal, req)


@router.get("/webhook_subscriptions", response_model=Page[schemas.WebhookSubscriptionOut])
async def list_subscriptions(
    _principal: CurrentPrincipal,
    session: SessionDep,
    cursor: str | None = None,
    limit: int | None = Query(default=None, ge=1, le=200),
) -> Page[schemas.WebhookSubscriptionOut]:
    return await service.list_subscriptions(session, cursor=cursor, limit=limit)


@router.get(
    "/webhook_subscriptions/{subscription_id}", response_model=schemas.WebhookSubscriptionOut
)
async def get_subscription(
    subscription_id: str, _principal: CurrentPrincipal, session: SessionDep
) -> schemas.WebhookSubscriptionOut:
    return await service.get_subscription(session, subscription_id)


@router.patch(
    "/webhook_subscriptions/{subscription_id}", response_model=schemas.WebhookSubscriptionOut
)
async def update_subscription(
    subscription_id: str,
    req: schemas.WebhookSubscriptionUpdate,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> schemas.WebhookSubscriptionOut:
    return await service.update_subscription(session, principal, subscription_id, req)


@router.delete("/webhook_subscriptions/{subscription_id}", status_code=204)
async def delete_subscription(
    subscription_id: str, principal: CurrentPrincipal, session: SessionDep
) -> Response:
    await service.delete_subscription(session, principal, subscription_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/webhook_subscriptions/{subscription_id}/rotate-secret",
    response_model=schemas.WebhookSubscriptionCreated,
)
async def rotate_secret(
    subscription_id: str, principal: CurrentPrincipal, session: SessionDep
) -> schemas.WebhookSubscriptionCreated:
    return await service.rotate_secret(session, principal, subscription_id)


@router.get(
    "/webhook_subscriptions/{subscription_id}/deliveries",
    response_model=Page[schemas.WebhookDeliveryOut],
)
async def list_deliveries(
    subscription_id: str,
    _principal: CurrentPrincipal,
    session: SessionDep,
    cursor: str | None = None,
    limit: int | None = Query(default=None, ge=1, le=200),
) -> Page[schemas.WebhookDeliveryOut]:
    return await service.list_deliveries(session, subscription_id, cursor=cursor, limit=limit)


@router.get(
    "/webhook_subscriptions/{subscription_id}/deliveries/{delivery_id}",
    response_model=schemas.WebhookDeliveryOut,
)
async def get_delivery(
    subscription_id: str, delivery_id: str, _principal: CurrentPrincipal, session: SessionDep
) -> schemas.WebhookDeliveryOut:
    return await service.get_delivery(session, subscription_id, delivery_id)


@router.post(
    "/webhook_subscriptions/{subscription_id}/deliveries/{delivery_id}/redeliver",
    response_model=schemas.WebhookDeliveryOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def redeliver(
    subscription_id: str, delivery_id: str, principal: CurrentPrincipal, session: SessionDep
) -> schemas.WebhookDeliveryOut:
    return await service.redeliver(session, principal, subscription_id, delivery_id)
