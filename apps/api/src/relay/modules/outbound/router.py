"""HTTP routes for the ``outbound`` module (P1.8). Mounted by relay.main under ``/v0``.

Admin surfaces (subscription types, consent) run as a ``Principal`` with RBAC in the service layer.
The unsubscribe surface (``/outbound/u/{token}``) is **unauthenticated**: it authenticates via the
stateless HMAC token and self-scopes RLS from the token's workspace id (mirrors ``/widget/boot``).
The GET renders a confirmation page and performs **no** state change (mail scanners prefetch GET
links); all mutation is the RFC 8058 one-click POST.
"""

from __future__ import annotations

import html

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse

from relay.core.db import session_scope
from relay.core.deps import ContactSession, CurrentPrincipal, SessionDep
from relay.core.idempotency import idempotent

from . import schemas, service
from .unsubscribe_token import parse_unsubscribe_token

router = APIRouter()


# --- Subscription types (admin) ----------------------------------------------------------------


@router.post(
    "/outbound/subscription-types", response_model=schemas.SubscriptionTypeOut, status_code=201
)
async def create_subscription_type(
    req: schemas.SubscriptionTypeCreate, principal: CurrentPrincipal, session: SessionDep
) -> schemas.SubscriptionTypeOut:
    return await service.create_subscription_type(session, principal, req)


@router.get("/outbound/subscription-types", response_model=list[schemas.SubscriptionTypeOut])
async def list_subscription_types(
    principal: CurrentPrincipal, session: SessionDep, include_archived: bool = False
) -> list[schemas.SubscriptionTypeOut]:
    return await service.list_subscription_types(
        session, principal, include_archived=include_archived
    )


@router.delete("/outbound/subscription-types/{subscription_type_id}", status_code=204)
async def archive_subscription_type(
    subscription_type_id: str, principal: CurrentPrincipal, session: SessionDep
) -> Response:
    await service.archive_subscription_type(session, principal, subscription_type_id)
    return Response(status_code=204)


# --- Consent (admin) ---------------------------------------------------------------------------


@router.put("/outbound/contacts/{contact_id}/consent", response_model=schemas.ConsentOut)
async def set_consent(
    contact_id: str,
    req: schemas.ConsentSetIn,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> schemas.ConsentOut:
    return await service.set_consent_admin(session, principal, contact_id, req)


@router.get("/outbound/contacts/{contact_id}/consents", response_model=list[schemas.ConsentOut])
async def list_consents(
    contact_id: str, principal: CurrentPrincipal, session: SessionDep
) -> list[schemas.ConsentOut]:
    return await service.list_consents(session, principal, contact_id)


# --- Email broadcasts (campaigns) --------------------------------------------------------------


@router.post("/outbound/campaigns", response_model=schemas.CampaignOut, status_code=201)
@idempotent(status_code=201)
async def create_campaign(
    req: schemas.CampaignCreate,
    request: Request,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> schemas.CampaignOut:
    return await service.create_campaign(session, principal, req)


@router.get("/outbound/campaigns", response_model=list[schemas.CampaignOut])
async def list_campaigns(
    principal: CurrentPrincipal, session: SessionDep
) -> list[schemas.CampaignOut]:
    return await service.list_campaigns(session)


@router.get("/outbound/campaigns/{campaign_id}", response_model=schemas.CampaignOut)
async def get_campaign(
    campaign_id: str, principal: CurrentPrincipal, session: SessionDep
) -> schemas.CampaignOut:
    return await service.get_campaign(session, campaign_id)


@router.get("/outbound/campaigns/{campaign_id}/stats", response_model=schemas.CampaignStatsOut)
async def get_campaign_stats(
    campaign_id: str, principal: CurrentPrincipal, session: SessionDep
) -> schemas.CampaignStatsOut:
    return await service.get_campaign_stats(session, campaign_id)


@router.post("/outbound/campaigns/{campaign_id}/fire", response_model=schemas.CampaignOut)
@idempotent(status_code=200)
async def fire_campaign(
    campaign_id: str,
    request: Request,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> schemas.CampaignOut:
    return await service.fire_campaign(session, principal, campaign_id)


# --- In-app posts & chats --------------------------------------------------------------------


@router.post("/outbound/posts", response_model=schemas.PostOut, status_code=201)
@idempotent(status_code=201)
async def create_post(
    req: schemas.PostCreate,
    request: Request,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> schemas.PostOut:
    return await service.create_post(session, principal, req)


@router.get("/outbound/posts", response_model=list[schemas.PostOut])
async def list_posts(principal: CurrentPrincipal, session: SessionDep) -> list[schemas.PostOut]:
    return await service.list_posts(session)


@router.get("/outbound/posts/{post_id}", response_model=schemas.PostOut)
async def get_post(
    post_id: str, principal: CurrentPrincipal, session: SessionDep
) -> schemas.PostOut:
    return await service.get_post(session, post_id)


@router.post("/outbound/posts/{post_id}/fire", response_model=schemas.PostOut)
@idempotent(status_code=200)
async def fire_post(
    post_id: str, request: Request, principal: CurrentPrincipal, session: SessionDep
) -> schemas.PostOut:
    return await service.fire_post(session, principal, post_id)


# Widget (contact) engagement — mark a delivered post seen/clicked.
@router.post("/outbound/receipts/{receipt_id}/seen", status_code=204)
async def mark_post_seen(
    receipt_id: str, principal: ContactSession, session: SessionDep
) -> Response:
    await service.mark_post_seen(session, principal.contact_id, receipt_id)
    return Response(status_code=204)


@router.post("/outbound/receipts/{receipt_id}/click", status_code=204)
async def mark_post_clicked(
    receipt_id: str, principal: ContactSession, session: SessionDep
) -> Response:
    await service.mark_post_clicked(session, principal.contact_id, receipt_id)
    return Response(status_code=204)


# --- Public unsubscribe (unauthenticated; token-scoped) ----------------------------------------

_NEUTRAL_PAGE = (
    "<!doctype html><html><head><meta charset='utf-8'><title>Unsubscribe</title></head>"
    "<body style='font-family:system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem'>"
    "<h1>Unsubscribe</h1><p>This unsubscribe link is invalid or has expired.</p></body></html>"
)


def _confirm_page(token: str, subscription_name: str) -> str:
    safe = html.escape(subscription_name)
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Unsubscribe</title></head>"
        "<body style='font-family:system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem'>"
        f"<h1>Unsubscribe from {safe}?</h1>"
        "<p>Click the button to stop receiving these messages.</p>"
        f"<form method='post' action='/v0/outbound/u/{html.escape(token)}'>"
        "<input type='hidden' name='List-Unsubscribe' value='One-Click'>"
        "<button type='submit' style='padding:.6rem 1.2rem;font-size:1rem'>Unsubscribe</button>"
        "</form></body></html>"
    )


def _done_page(subscription_name: str) -> str:
    safe = html.escape(subscription_name)
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Unsubscribed</title></head>"
        "<body style='font-family:system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem'>"
        f"<h1>You're unsubscribed</h1><p>You will no longer receive {safe} messages.</p>"
        "</body></html>"
    )


@router.get("/outbound/u/{token}", response_class=HTMLResponse)
async def unsubscribe_page(token: str) -> HTMLResponse:
    """Render a confirmation page (no state change — GET must be safe; scanners prefetch it)."""
    parsed = parse_unsubscribe_token(token)
    if parsed is None:
        return HTMLResponse(_NEUTRAL_PAGE)
    workspace_id, _contact_id, subscription_type_id = parsed
    async with session_scope(workspace_id) as session:
        name = await service.describe_unsubscribe(session, subscription_type_id)
    if name is None:
        return HTMLResponse(_NEUTRAL_PAGE)
    return HTMLResponse(_confirm_page(token, name))


@router.post("/outbound/u/{token}", response_class=HTMLResponse)
async def unsubscribe_one_click(token: str, request: Request) -> HTMLResponse:
    """RFC 8058 one-click (and the confirmation form) — sets consent to ``unsubscribed``."""
    parsed = parse_unsubscribe_token(token)
    if parsed is None:
        return HTMLResponse(_NEUTRAL_PAGE, status_code=200)
    workspace_id, contact_id, subscription_type_id = parsed
    form = await request.form()
    source = (
        "list_unsubscribe" if form.get("List-Unsubscribe") == "One-Click" else "unsubscribe_page"
    )
    detail = {
        "ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }
    async with session_scope(workspace_id) as session:
        name = await service.apply_unsubscribe(
            session,
            workspace_id=workspace_id,
            contact_id=contact_id,
            subscription_type_id=subscription_type_id,
            source=source,
            detail=detail,
        )
    return HTMLResponse(_done_page(name) if name else _NEUTRAL_PAGE)
