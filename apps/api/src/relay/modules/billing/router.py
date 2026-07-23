"""HTTP routes for the `billing` module (RFC-002 §5.6, P0.10). Mounted under ``/v0``.

The checkout/portal endpoints deliberately don't take ``SessionDep`` — that dependency holds
a transaction open for the whole request, and the acceptance criterion is that no Stripe call
happens inside a request-path transaction. ``service.create_checkout_session`` /
``create_portal_session`` open their own short read-only session internally, close it, and
only then call Stripe (see ``service.py`` module docstring).

The webhook endpoint is unauthenticated (Stripe signs the payload itself, verified in
``service.handle_webhook_event``) and needs the raw body, so it bypasses the JSON body
parsing FastAPI would otherwise do.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from relay.core.deps import CurrentPrincipal, SessionDep

from . import schemas, service

router = APIRouter(tags=["billing"])


@router.post("/billing/checkout-session", response_model=schemas.CheckoutSessionOut)
async def create_checkout_session(
    req: schemas.CheckoutSessionCreate, principal: CurrentPrincipal
) -> schemas.CheckoutSessionOut:
    return await service.create_checkout_session(principal, req)


@router.post("/billing/portal-session", response_model=schemas.PortalSessionOut)
async def create_portal_session(principal: CurrentPrincipal) -> schemas.PortalSessionOut:
    return await service.create_portal_session(principal)


@router.get("/billing/subscription", response_model=schemas.SubscriptionOut)
async def get_subscription(
    principal: CurrentPrincipal, session: SessionDep
) -> schemas.SubscriptionOut:
    return await service.get_billing_summary(session, principal)


@router.post("/billing/webhook", status_code=status.HTTP_204_NO_CONTENT)
async def stripe_webhook(request: Request) -> Response:
    payload = await request.body()
    await service.handle_webhook_event(
        payload=payload, sig_header=request.headers.get("Stripe-Signature")
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
