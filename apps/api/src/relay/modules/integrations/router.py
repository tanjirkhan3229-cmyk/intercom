"""HTTP routes for the ``integrations`` module (P1.9). Mounted by relay.main under ``/v0``.

Three surfaces:
- **Admin config** (JWT, ADMIN via the service choke point): connect/list/get/pause/disconnect.
- **Slack inbound** ``POST /integrations/slack/events`` — UNAUTHENTICATED like the SES/SNS handler;
  authenticity is the Slack request signature (verified against the resolved workspace's secret),
  not a JWT. Mirrors channels/email/inbound.
- **Zapier** (API-key principals; allowlisted in core/public_api): auth test + REST-hook
  subscribe/unsubscribe. Zapier "actions" reuse the existing contacts/conversations public routes.
"""

from __future__ import annotations

import datetime as dt
import json

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse

from relay.core.crypto import InvalidToken, decrypt_secret
from relay.core.deps import CurrentPrincipal, SessionDep
from relay.core.logging import get_logger
from relay.settings import get_settings
from relay.worker import celery_app

from . import schemas, service, slack_sign

router = APIRouter(tags=["integrations"])
log = get_logger(__name__)


# --- Slack admin config -------------------------------------------------------


@router.post("/integrations/slack", response_model=schemas.IntegrationOut, status_code=201)
async def connect_slack(
    req: schemas.SlackConnect, principal: CurrentPrincipal, session: SessionDep
) -> schemas.IntegrationOut:
    return await service.connect_slack(session, principal, req)


@router.get("/integrations", response_model=list[schemas.IntegrationOut])
async def list_integrations(
    _principal: CurrentPrincipal, session: SessionDep
) -> list[schemas.IntegrationOut]:
    return await service.list_integrations(session)


@router.get("/integrations/{integration_id}", response_model=schemas.IntegrationOut)
async def get_integration(
    integration_id: str, _principal: CurrentPrincipal, session: SessionDep
) -> schemas.IntegrationOut:
    return await service.get_integration(session, integration_id)


@router.patch("/integrations/{integration_id}/status", response_model=schemas.IntegrationOut)
async def set_status(
    integration_id: str,
    req: schemas.IntegrationStatusUpdate,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> schemas.IntegrationOut:
    return await service.set_integration_status(session, principal, integration_id, req)


@router.delete("/integrations/{integration_id}", status_code=204)
async def delete_integration(
    integration_id: str, principal: CurrentPrincipal, session: SessionDep
) -> Response:
    await service.delete_integration(session, principal, integration_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Slack inbound (unauthenticated; signature-verified) ----------------------


@router.post("/integrations/slack/events")
async def slack_events(request: Request) -> Response:
    """Slack Events API callback. Verifies the Slack request signature against the resolved
    workspace's signing secret, then fast-acks and enqueues ingestion (Slack needs a 2xx within 3s).
    """
    raw = await request.body()
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    # URL-verification handshake (one-time, at Event-URL setup): echo the challenge.
    if data.get("type") == "url_verification":
        return JSONResponse({"challenge": data.get("challenge")})

    team_id = data.get("team_id")
    if not isinstance(team_id, str):
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    resolved = await service.resolve_slack_account_by_team(team_id)
    if resolved is None:
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    workspace_id, signing_ct = resolved
    try:
        signing_secret = decrypt_secret(signing_ct)
    except InvalidToken:
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    try:
        ts = int(request.headers.get(slack_sign.TIMESTAMP_HEADER, ""))
    except ValueError:
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    ok = slack_sign.verify_signature(
        signing_secret,
        timestamp=ts,
        body=raw,
        header=request.headers.get(slack_sign.SIGNATURE_HEADER, ""),
        tolerance_seconds=get_settings().slack_signature_tolerance_seconds,
        now=int(dt.datetime.now(dt.UTC).timestamp()),
    )
    if not ok:
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    celery_app.send_task(
        "integrations.slack_ingest_inbound",
        args=[str(workspace_id), raw.decode("utf-8")],
        queue="ingest",
    )
    return Response(status_code=status.HTTP_200_OK)


# --- Zapier (API-key principals) ----------------------------------------------


@router.get("/zapier/auth/test", response_model=schemas.ZapierAuthTestOut)
async def zapier_auth_test(
    principal: CurrentPrincipal, _session: SessionDep
) -> schemas.ZapierAuthTestOut:
    return service.zapier_auth_test(principal)


@router.post("/zapier/subscriptions", response_model=schemas.ZapierSubscribeOut, status_code=201)
async def zapier_subscribe(
    req: schemas.ZapierSubscribe, principal: CurrentPrincipal, session: SessionDep
) -> schemas.ZapierSubscribeOut:
    return await service.zapier_subscribe(session, principal, req)


@router.delete("/zapier/subscriptions/{subscription_id}", status_code=204)
async def zapier_unsubscribe(
    subscription_id: str, _principal: CurrentPrincipal, session: SessionDep
) -> Response:
    await service.zapier_unsubscribe(session, subscription_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
