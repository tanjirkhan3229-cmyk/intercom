"""HTTP routes for the ``channels`` (email) module. Mounted by relay.main under ``/v0``.

Two unauthenticated webhooks (SNS-signature-verified) receive SES inbound + bounce/complaint
notifications and hand off to Celery — they never touch tenant tables (no principal → RLS would
return nothing anyway). The admin/agent management routes follow the standard
``CurrentPrincipal`` + ``SessionDep`` pattern; RBAC is enforced in the service layer.
"""

from __future__ import annotations

import json

import httpx
from fastapi import APIRouter, Query, Request, Response, status

from relay.core.deps import CurrentPrincipal, SessionDep
from relay.core.errors import PermissionDeniedError
from relay.core.logging import get_logger
from relay.core.pagination import Page
from relay.settings import get_settings

from . import schemas, service, sns

log = get_logger(__name__)

router = APIRouter(tags=["channels"])


# --- Inbound webhooks (unauthenticated; SNS-verified) -------------------------


async def _verified_envelope(request: Request) -> sns.SnsEnvelope | None:
    """Parse + verify an SNS POST; auto-confirm subscriptions. Returns the envelope for a
    Notification to process, or ``None`` when there's nothing further to do (confirmation/handled).
    Raises 403 on an invalid signature."""
    body = await request.json()
    envelope = sns.parse_envelope(body)
    async with httpx.AsyncClient(timeout=5.0) as client:
        if get_settings().sns_verify_signatures and not await sns.verify(body, client=client):
            raise PermissionDeniedError("invalid SNS signature")
        if envelope.type == "SubscriptionConfirmation":
            await sns.confirm_subscription(envelope, client=client)
            return None
    if envelope.type != "Notification":
        return None
    return envelope


@router.post("/channels/email/inbound", status_code=status.HTTP_202_ACCEPTED)
async def inbound_webhook(request: Request) -> Response:
    """SES receipt → S3 raw MIME → SNS. Enqueue the ingest task with the S3 ref + SNS MessageId."""
    from .tasks import ingest_email

    envelope = await _verified_envelope(request)
    if envelope is None:
        return Response(status_code=status.HTTP_200_OK)

    ses = json.loads(envelope.message or "{}")
    receipt = ses.get("receipt", {})
    action = receipt.get("action", {})
    bucket = action.get("bucketName")
    key = action.get("objectKey")
    if not bucket or not key:
        log.warning("channels.inbound.no_s3_action", message_id=envelope.message_id)
        return Response(status_code=status.HTTP_200_OK)

    # ``receipt.recipients`` is the trusted SES envelope recipient list — the only safe routing key
    # (header To/Cc are sender-forgeable). Passed through so ingest never routes on Cc.
    recipients = [r for r in receipt.get("recipients", []) if isinstance(r, str)]
    ingest_email.apply_async(
        kwargs={
            "sns_message_id": envelope.message_id,
            "s3_bucket": bucket,
            "s3_key": key,
            "recipients": recipients,
        }
    )
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/channels/email/events", status_code=status.HTTP_202_ACCEPTED)
async def events_webhook(request: Request) -> Response:
    """SES configuration-set bounce/complaint events (via SNS) → suppression."""
    from .tasks import record_ses_event

    envelope = await _verified_envelope(request)
    if envelope is None:
        return Response(status_code=status.HTTP_200_OK)
    record_ses_event.apply_async(kwargs={"message_json": envelope.message})
    return Response(status_code=status.HTTP_202_ACCEPTED)


# --- Domains ------------------------------------------------------------------


@router.post(
    "/channels/email/domains",
    response_model=schemas.DomainOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_domain(
    req: schemas.DomainCreate, principal: CurrentPrincipal, session: SessionDep
) -> schemas.DomainOut:
    return await service.create_domain(session, principal, req)


@router.get("/channels/email/domains", response_model=list[schemas.DomainOut])
async def list_domains(principal: CurrentPrincipal, session: SessionDep) -> list[schemas.DomainOut]:
    return await service.list_domains(session)


@router.post("/channels/email/domains/{domain_id}/verify", response_model=schemas.DomainOut)
async def verify_domain(
    domain_id: str, principal: CurrentPrincipal, session: SessionDep
) -> schemas.DomainOut:
    return await service.verify_domain(session, principal, domain_id)


# --- Channel accounts (inbound addresses) -------------------------------------


@router.post(
    "/channels/email/accounts",
    response_model=schemas.AccountOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_account(
    req: schemas.AccountCreate, principal: CurrentPrincipal, session: SessionDep
) -> schemas.AccountOut:
    return await service.create_account(session, principal, req)


@router.get("/channels/email/accounts", response_model=list[schemas.AccountOut])
async def list_accounts(
    principal: CurrentPrincipal, session: SessionDep
) -> list[schemas.AccountOut]:
    return await service.list_accounts(session)


@router.post("/channels/email/accounts/{account_id}/status", response_model=schemas.AccountOut)
async def set_account_status(
    account_id: str,
    req: schemas.AccountStatusUpdate,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> schemas.AccountOut:
    return await service.set_account_status(session, principal, account_id, req)


# --- Suppressions -------------------------------------------------------------


@router.get("/channels/email/suppressions", response_model=Page[schemas.SuppressionOut])
async def list_suppressions(
    principal: CurrentPrincipal,
    session: SessionDep,
    cursor: str | None = None,
    limit: int | None = Query(default=None, ge=1, le=200),
) -> Page[schemas.SuppressionOut]:
    return await service.list_suppressions(session, cursor=cursor, limit=limit)


@router.post(
    "/channels/email/suppressions",
    response_model=schemas.SuppressionOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_suppression(
    req: schemas.SuppressionCreate, principal: CurrentPrincipal, session: SessionDep
) -> schemas.SuppressionOut:
    return await service.add_suppression(session, principal, req)


@router.delete(
    "/channels/email/suppressions/{suppression_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_suppression(
    suppression_id: str, principal: CurrentPrincipal, session: SessionDep
) -> Response:
    await service.remove_suppression(session, principal, suppression_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
