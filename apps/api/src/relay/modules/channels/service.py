"""Service layer for the ``channels`` module — the email adapter (P0.7, RFC-001 §6.6, §9).

The ONLY surface other modules import (plus ``events``). Reaching into another module's
``models``/``router`` is forbidden (import-linter). This module, conversely, reaches OUT only via
``crm.service`` / ``messaging.service`` (the sanctioned cross-module channels).

Two flows:
- **Inbound** (``ingest``): SNS-MessageId dedupe (pre-tenancy, global) → parse → resolve
  workspace+conversation (stateless reply token → In-Reply-To → recipient address) → RLS GUC →
  resolve contact → create/append conversation via ``messaging.service`` → persist the email ledger
  row. Sender is authenticated against the thread's contact before appending.
- **Outbound** (``send_email``): exactly-once gate on ``email_messages(workspace_id, part_id)`` →
  suppression / pause / rate / size checks → render threaded MIME → send via the breaker-wrapped
  transport → record the ledger row + delivery event.
"""

from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import boto3
import sqlalchemy as sa
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from relay.core import outbox, storage
from relay.core.db import session_scope, set_workspace_guc
from relay.core.errors import ConflictError, NotFoundError, RateLimitedError, ValidationError
from relay.core.ids import IdPrefix, decode_public_id, encode_public_id, uuid7
from relay.core.logging import get_logger
from relay.core.pagination import Page, clamp_limit
from relay.core.principal import Principal
from relay.core.rbac import Role, authorize
from relay.core.redis import get_redis
from relay.modules.crm import service as crm_service
from relay.modules.messaging import service as messaging_service
from relay.settings import get_settings

from . import events, mime, reply_token, schemas, sender
from .models import (
    ChannelAccount,
    EmailDeliveryEvent,
    EmailMessage,
    InboundDedupe,
    Suppression,
    VerifiedDomain,
)

log = get_logger(__name__)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# --- Clear, machine-readable errors (surfaced by the send task; asserted by tests) ------------


class SuppressedRecipient(ConflictError):
    code = "recipient_suppressed"


class MessageTooLarge(ValidationError):
    code = "message_too_large"


class UnroutableEmail(ValidationError):
    code = "unroutable_email"


# --- DTO builders -------------------------------------------------------------


def domain_out(d: VerifiedDomain) -> schemas.DomainOut:
    return schemas.DomainOut(
        id=encode_public_id(IdPrefix.DOMAIN, d.id),
        domain=d.domain,
        status=d.status,
        spf_ok=d.spf_ok,
        dmarc_ok=d.dmarc_ok,
        dns_records=list(d.dns_records),
        verified_at=d.verified_at,
        created_at=d.created_at,
    )


def account_out(a: ChannelAccount) -> schemas.AccountOut:
    return schemas.AccountOut(
        id=encode_public_id(IdPrefix.CHANNEL_ACCOUNT, a.id),
        address=a.address,
        domain_id=encode_public_id(IdPrefix.DOMAIN, a.domain_id) if a.domain_id else None,
        status=a.status,
        created_at=a.created_at,
    )


def suppression_out(s: Suppression) -> schemas.SuppressionOut:
    return schemas.SuppressionOut(
        id=encode_public_id(IdPrefix.SUPPRESSION, s.id),
        email=s.email,
        reason=s.reason,
        source=s.source,
        created_at=s.created_at,
    )


def _decode_or_404(prefix: str, public_id: str, what: str) -> uuid.UUID:
    try:
        return decode_public_id(prefix, public_id)
    except ValueError as exc:
        raise NotFoundError(f"{what} not found") from exc


# --- Domains ------------------------------------------------------------------


def _dns_records(domain: str, token: str) -> list[dict[str, Any]]:
    """The DNS records a tenant must publish (ownership TXT + SPF + DMARC). DKIM CNAMEs are added
    once SES creates the sending identity (populated by the verification flow)."""
    return [
        {"type": "TXT", "name": f"_relay-verify.{domain}", "value": token, "purpose": "ownership"},
        {
            "type": "TXT",
            "name": domain,
            "value": "v=spf1 include:amazonses.com ~all",
            "purpose": "spf",
        },
        {
            "type": "TXT",
            "name": f"_dmarc.{domain}",
            "value": "v=DMARC1; p=none;",
            "purpose": "dmarc",
        },
    ]


async def create_domain(
    session: AsyncSession, principal: Principal, req: schemas.DomainCreate
) -> schemas.DomainOut:
    authorize(principal, min_role=Role.ADMIN)
    domain = req.domain.strip().lower()
    token = uuid7().hex
    d = VerifiedDomain(
        workspace_id=principal.workspace_id,
        domain=domain,
        status="pending",
        verification_token=token,
        dns_records=_dns_records(domain, token),
    )
    session.add(d)
    try:
        await session.flush()
    except sa.exc.IntegrityError as exc:
        raise ConflictError("this domain is already added to your workspace") from exc
    return domain_out(d)


async def list_domains(session: AsyncSession) -> list[schemas.DomainOut]:
    rows = (await session.scalars(select(VerifiedDomain).order_by(VerifiedDomain.created_at))).all()
    return [domain_out(d) for d in rows]


async def _load_domain(session: AsyncSession, domain_id: uuid.UUID) -> VerifiedDomain:
    d = await session.get(VerifiedDomain, domain_id)
    if d is None:
        raise NotFoundError("domain not found")
    return d


def check_domain_verified(domain: str) -> bool:
    """Query SES for a domain's verification status. Monkeypatched in tests (no AWS in CI)."""
    s = get_settings()
    client = boto3.client(
        "ses",
        region_name=s.ses_region,
        endpoint_url=s.ses_endpoint_url,
        aws_access_key_id=s.ses_access_key_id,
        aws_secret_access_key=s.ses_secret_access_key,
    )
    attrs = client.get_identity_verification_attributes(Identities=[domain])
    status = attrs.get("VerificationAttributes", {}).get(domain, {}).get("VerificationStatus")
    return bool(status == "Success")


async def _mark_verified(session: AsyncSession, d: VerifiedDomain) -> None:
    d.status = "verified"
    d.spf_ok = True
    d.dmarc_ok = True
    d.verified_at = _now()
    try:
        await session.flush()
    except sa.exc.IntegrityError as exc:
        # Global partial-unique on (domain) WHERE status='verified' — another tenant owns it.
        # Generic message so we don't leak that another workspace claimed the domain.
        raise ValidationError("this domain is unavailable for verification") from exc
    await outbox.emit(
        session,
        aggregate=events.AGGREGATE_EMAIL_DOMAIN,
        aggregate_id=d.id,
        topic=events.EMAIL_DOMAIN_VERIFIED,
        payload={
            "workspace_id": encode_public_id(IdPrefix.WORKSPACE, d.workspace_id),
            "domain_id": encode_public_id(IdPrefix.DOMAIN, d.id),
            "domain": d.domain,
        },
    )


async def verify_domain(
    session: AsyncSession,
    principal: Principal,
    public_id: str,
    *,
    checker: Callable[[str], bool] | None = None,
) -> schemas.DomainOut:
    """On-demand verification (the tenant's "Verify" action). Flips to ``verified`` when the DNS/
    SES check passes; otherwise leaves it ``pending`` with the DNS records to publish.

    ``checker`` is resolved at call time (default ``check_domain_verified``) so tests can
    monkeypatch the module attribute."""
    authorize(principal, min_role=Role.ADMIN)
    check = checker or check_domain_verified
    d = await _load_domain(session, _decode_or_404(IdPrefix.DOMAIN, public_id, "domain"))
    if d.status != "verified" and check(d.domain):
        await _mark_verified(session, d)
    return domain_out(d)


async def poll_pending_domains(*, checker: Callable[[str], bool] | None = None) -> int:
    """Beat-driven verification poller. Lists pending domains across tenants (SECURITY DEFINER),
    then verifies each under its own workspace GUC. Returns the count newly verified."""
    async with session_scope(None) as session:
        rows = (
            await session.execute(text("SELECT workspace_id, id FROM channels_pending_domains()"))
        ).all()
    check = checker or check_domain_verified
    verified = 0
    for ws_raw, dom_raw in rows:
        ws_id = uuid.UUID(str(ws_raw))
        dom_id = uuid.UUID(str(dom_raw))
        async with session_scope(ws_id) as session:
            d = await session.get(VerifiedDomain, dom_id)
            if d is None or d.status == "verified":
                continue
            if check(d.domain):
                await _mark_verified(session, d)
                verified += 1
    return verified


# --- Channel accounts (inbound addresses; pause switch) -----------------------


async def create_account(
    session: AsyncSession, principal: Principal, req: schemas.AccountCreate
) -> schemas.AccountOut:
    authorize(principal, min_role=Role.ADMIN)
    domain_id = _decode_or_404(IdPrefix.DOMAIN, req.domain_id, "domain") if req.domain_id else None
    account = ChannelAccount(
        workspace_id=principal.workspace_id,
        channel="email",
        address=str(req.address).strip().lower(),
        domain_id=domain_id,
        status="active",
    )
    session.add(account)
    try:
        await session.flush()
    except sa.exc.IntegrityError as exc:
        # Global-unique address: mask cross-tenant ownership behind a generic message.
        raise ConflictError("this address is unavailable") from exc
    return account_out(account)


async def list_accounts(session: AsyncSession) -> list[schemas.AccountOut]:
    rows = (await session.scalars(select(ChannelAccount).order_by(ChannelAccount.created_at))).all()
    return [account_out(a) for a in rows]


async def set_account_status(
    session: AsyncSession, principal: Principal, public_id: str, req: schemas.AccountStatusUpdate
) -> schemas.AccountOut:
    """Set an account's status. ``paused`` is the per-tenant send-pause switch (RFC-001 §9)."""
    authorize(principal, min_role=Role.ADMIN)
    account = await session.get(
        ChannelAccount, _decode_or_404(IdPrefix.CHANNEL_ACCOUNT, public_id, "account")
    )
    if account is None:
        raise NotFoundError("account not found")
    account.status = req.status
    await session.flush()
    return account_out(account)


# --- Suppressions -------------------------------------------------------------


async def is_suppressed(session: AsyncSession, workspace_id: uuid.UUID, email: str) -> bool:
    row = await session.scalar(
        select(Suppression.id).where(
            Suppression.workspace_id == workspace_id, Suppression.email == email
        )
    )
    return row is not None


async def suppress_address(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    email: str,
    reason: str,
    source: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Idempotently add a suppression (system path: SES bounce/complaint). Emits an event on first
    insert so P0.11 webhooks can react. Caller holds the RLS GUC for ``workspace_id``."""
    stmt = (
        pg_insert(Suppression)
        .values(
            id=uuid7(),
            workspace_id=workspace_id,
            email=email,
            reason=reason,
            source=source,
            detail=detail or {},
        )
        .on_conflict_do_nothing(index_elements=[Suppression.workspace_id, Suppression.email])
        .returning(Suppression.id)
    )
    new_id = (await session.execute(stmt)).scalar_one_or_none()
    await session.flush()
    if new_id is not None:
        await outbox.emit(
            session,
            aggregate=events.AGGREGATE_SUPPRESSION,
            aggregate_id=uuid.UUID(str(new_id)),
            topic=events.EMAIL_ADDRESS_SUPPRESSED,
            payload={
                "workspace_id": encode_public_id(IdPrefix.WORKSPACE, workspace_id),
                "email": email,
                "reason": reason,
            },
        )


async def add_suppression(
    session: AsyncSession, principal: Principal, req: schemas.SuppressionCreate
) -> schemas.SuppressionOut:
    authorize(principal, min_role=Role.AGENT)
    email = str(req.email).strip().lower()
    await suppress_address(
        session,
        workspace_id=principal.workspace_id,
        email=email,
        reason=req.reason,
        source="manual",
    )
    row = await session.scalar(
        select(Suppression).where(
            Suppression.workspace_id == principal.workspace_id, Suppression.email == email
        )
    )
    assert row is not None
    return suppression_out(row)


async def remove_suppression(session: AsyncSession, principal: Principal, public_id: str) -> None:
    authorize(principal, min_role=Role.ADMIN)
    sid = _decode_or_404(IdPrefix.SUPPRESSION, public_id, "suppression")
    row = await session.get(Suppression, sid)
    if row is None:
        raise NotFoundError("suppression not found")
    await session.delete(row)
    await session.flush()


async def list_suppressions(
    session: AsyncSession, *, cursor: str | None = None, limit: int | None = None
) -> Page[schemas.SuppressionOut]:
    n = clamp_limit(limit)
    stmt = select(Suppression).order_by(Suppression.id.desc())
    if cursor:
        stmt = stmt.where(Suppression.id < _decode_or_404(IdPrefix.SUPPRESSION, cursor, "cursor"))
    rows = list((await session.scalars(stmt.limit(n + 1))).all())
    next_cursor = None
    if len(rows) > n:
        rows = rows[:n]
        next_cursor = encode_public_id(IdPrefix.SUPPRESSION, rows[-1].id)
    return Page(items=[suppression_out(r) for r in rows], next_cursor=next_cursor)


# --- Inbound ingest -----------------------------------------------------------


@dataclass
class _Resolution:
    workspace_id: uuid.UUID
    conversation_id: uuid.UUID | None
    channel_account_id: uuid.UUID | None
    via: str  # 'token' | 'in_reply_to' | 'address'


def _synth_message_id(raw: bytes) -> str:
    """Deterministic Message-ID when the email omits one (Postgres won't dedupe NULLs)."""
    return f"<{hashlib.sha256(raw).hexdigest()}@synthesized.relay>"


def _reply_token_of(addr: str) -> str | None:
    local, _, domain = addr.partition("@")
    if domain.lower() != get_settings().email_inbound_domain.lower():
        return None
    if not local.lower().startswith("reply+"):
        return None
    return local[len("reply+") :]


def _is_reply_address(addr: str) -> bool:
    local, _, _domain = addr.partition("@")
    return local.lower().startswith("reply+")


def _thread_candidates(parsed: mime.ParsedEmail) -> list[str]:
    candidates: list[str] = []
    if parsed.in_reply_to:
        candidates.append(parsed.in_reply_to)
    candidates.extend(reversed(parsed.references))  # newest reference first, after In-Reply-To
    seen: set[str] = set()
    unique: list[str] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


async def _resolve_by_address(session: AsyncSession, route_addrs: list[str]) -> _Resolution | None:
    for addr in route_addrs:
        if _is_reply_address(addr):
            continue
        row = (
            await session.execute(
                text(
                    "SELECT workspace_id, channel_account_id "
                    "FROM channels_resolve_inbound_address(:a)"
                ),
                {"a": addr},
            )
        ).first()
        if row is not None:
            return _Resolution(
                workspace_id=uuid.UUID(str(row[0])),
                conversation_id=None,
                channel_account_id=uuid.UUID(str(row[1])),
                via="address",
            )
    return None


async def _resolve_inbound(
    session: AsyncSession, parsed: mime.ParsedEmail, route_addrs: list[str]
) -> _Resolution | None:
    """Resolve the workspace/conversation. ``route_addrs`` are the TRUSTED recipients (the SES
    envelope ``receipt.recipients`` in production) — NOT the header To/Cc, which are forgeable and
    would let a stranger Cc a victim's inbound address to inject a cross-tenant conversation."""
    # 1) stateless reply token (primary; no DB lookup, no pre-tenancy RLS problem)
    for addr in route_addrs:
        token = _reply_token_of(addr)
        if token:
            decoded = reply_token.parse_reply_token(token)
            if decoded is not None:
                ws, conv = decoded
                return _Resolution(ws, conv, None, "token")
    # 2) In-Reply-To / References → an outbound message we sent (SECURITY DEFINER, cross-tenant)
    for mid in _thread_candidates(parsed):
        row = (
            await session.execute(
                text(
                    "SELECT workspace_id, conversation_id "
                    "FROM channels_resolve_outbound_message(:m)"
                ),
                {"m": mid},
            )
        ).first()
        if row is not None:
            return _Resolution(
                workspace_id=uuid.UUID(str(row[0])),
                conversation_id=uuid.UUID(str(row[1])),
                channel_account_id=None,
                via="in_reply_to",
            )
    # 3) new thread — resolve by trusted recipient address
    return await _resolve_by_address(session, route_addrs)


async def _sender_matches(
    session: AsyncSession, conversation_id: uuid.UUID, from_addr: str
) -> bool:
    owner = await messaging_service.conversation_contact_id(session, conversation_id)
    if owner is None:
        return False
    owner_email, _name = await crm_service.contact_email(session, owner)
    return bool(owner_email and owner_email.lower() == from_addr.lower())


async def _account_for_recipients(session: AsyncSession, to_addrs: list[str]) -> uuid.UUID | None:
    for addr in to_addrs:
        if _is_reply_address(addr):
            continue
        row = await session.scalar(select(ChannelAccount.id).where(ChannelAccount.address == addr))
        if row is not None:
            return row
    return None


def _store_attachments(
    workspace_public_id: str, parsed: mime.ParsedEmail
) -> tuple[list[dict[str, Any]], int]:
    """Store inbound attachments to S3 (worker-side). Drops any that would push the running total
    past the size cap; returns (stored_refs, dropped_count)."""
    out: list[dict[str, Any]] = []
    dropped = 0
    total = 0
    cap = get_settings().email_max_message_bytes
    for att in parsed.attachments:
        total += len(att.content)
        if total > cap:
            dropped += 1
            continue
        key = storage.build_key(workspace_public_id, uuid7().hex, att.filename)
        storage.put_object(
            get_settings().s3_bucket_attachments, key, att.content, content_type=att.content_type
        )
        out.append(
            {
                "key": key,
                "filename": att.filename,
                "content_type": att.content_type,
                "size": len(att.content),
            }
        )
    return out, dropped


async def ingest(
    *,
    sns_message_id: str,
    s3_bucket: str,
    s3_key: str,
    recipients: list[str] | None = None,
    fetch: Callable[[str, str], bytes] = storage.get_object,
) -> str:
    """Ingest one inbound email. Idempotent by SNS MessageId (primary) + RFC-822 Message-ID
    (secondary). Runs in ONE transaction so a transient failure rolls back the dedupe claim and a
    retry reprocesses. Raises ``UnroutableEmail`` for permanent failures (the task DLQs those).

    ``recipients`` are the TRUSTED SES envelope recipients (``receipt.recipients``); routing uses
    them, never the forgeable header To/Cc. It falls back to parsed headers only for the dev/test
    injection path where no SES envelope is available."""
    async with session_scope(None) as session:
        # Primary idempotency gate (global, pre-tenancy): claim the SNS MessageId.
        claimed = (
            await session.execute(
                pg_insert(InboundDedupe)
                .values(sns_message_id=sns_message_id)
                .on_conflict_do_nothing(index_elements=[InboundDedupe.sns_message_id])
                .returning(InboundDedupe.sns_message_id)
            )
        ).scalar_one_or_none()
        if claimed is None:
            log.info("channels.ingest.duplicate_sns", sns_message_id=sns_message_id)
            return "duplicate_sns"

        raw = fetch(s3_bucket, s3_key)
        parsed = mime.parse(raw)
        if not parsed.from_addr:
            raise UnroutableEmail("inbound email has no From address")

        route_addrs = [a for a in (recipients or parsed.to_addrs) if a]
        resolution = await _resolve_inbound(session, parsed, route_addrs)
        if resolution is None:
            raise UnroutableEmail(f"no inbound route for recipients {route_addrs!r}")

        ws_id = resolution.workspace_id
        await set_workspace_guc(session, ws_id)

        message_id = parsed.message_id or _synth_message_id(raw)
        dup = await session.scalar(
            select(EmailMessage.id).where(
                EmailMessage.workspace_id == ws_id, EmailMessage.message_id == message_id
            )
        )
        if dup is not None:
            log.info("channels.ingest.duplicate_message_id", message_id=message_id)
            return "duplicate_message_id"

        conversation_id = resolution.conversation_id
        channel_account_id = resolution.channel_account_id
        # Authenticate the sender against the thread's contact before appending — a leaked reply
        # address must not let a stranger inject into an existing conversation.
        if (
            conversation_id is not None
            and resolution.via in ("token", "in_reply_to")
            and not await _sender_matches(session, conversation_id, parsed.from_addr)
        ):
            log.warning("channels.ingest.sender_mismatch", conversation_id=str(conversation_id))
            conversation_id = None  # start a new thread for the actual sender (no injection)
            channel_account_id = await _account_for_recipients(session, route_addrs)

        body = parsed.text
        attachments, dropped = _store_attachments(
            encode_public_id(IdPrefix.WORKSPACE, ws_id), parsed
        )
        if dropped:
            body = (body or "") + f"\n\n[{dropped} attachment(s) omitted: exceeded size limit]"
        channel_meta = {
            "message_id": message_id,
            "subject": parsed.subject,
            "from": parsed.from_addr,
            "to": parsed.to_addrs,
            "in_reply_to": parsed.in_reply_to,
        }

        if conversation_id is None:
            contact_id = await crm_service.resolve_contact_email(
                session, workspace_id=ws_id, email=parsed.from_addr, name=parsed.from_name or None
            )
            conv = await messaging_service.open_email_conversation(
                session,
                workspace_id=ws_id,
                contact_id=contact_id,
                channel_account_id=channel_account_id,
                body=body,
                attachments=attachments,
                channel_meta=channel_meta,
            )
            conversation_id = conv.id
        else:
            await messaging_service.append_contact_email(
                session,
                conversation_id=conversation_id,
                body=body,
                attachments=attachments,
                channel_meta=channel_meta,
            )

        session.add(
            EmailMessage(
                workspace_id=ws_id,
                conversation_id=conversation_id,
                part_id=None,
                direction="in",
                message_id=message_id,
                in_reply_to=parsed.in_reply_to,
                email_references=parsed.references or None,
                s3_raw_key=s3_key,
                from_addr=parsed.from_addr,
                to_addr=parsed.to_addrs[0] if parsed.to_addrs else None,
                subject=parsed.subject,
            )
        )
        await session.flush()
        return "ingested"


# --- Outbound send ------------------------------------------------------------


async def _record_event(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    part_id: uuid.UUID | None,
    email: str | None,
    event: str,
    detail: dict[str, Any] | None = None,
) -> None:
    session.add(
        EmailDeliveryEvent(
            workspace_id=workspace_id,
            part_id=part_id,
            email=email,
            event=event,
            detail=detail or {},
        )
    )
    await session.flush()


async def _throttle(workspace_id: uuid.UUID) -> None:
    """Global fixed-window send-rate cap (RFC-001 §9). Raises ``RateLimitedError`` when exceeded,
    which the send task treats as transient (retry with backoff). No-op when unset."""
    limit = get_settings().email_send_rate_per_sec
    if not limit:
        return
    redis = get_redis()
    key = "email:rate:global"
    n = await redis.incr(key)
    # EXPIRE ... NX sets the 1s window TTL only if the key has none yet — crash-safe (unlike an
    # ``if n == 1`` guard, which can leave a TTL-less key wedged if the process dies mid-window).
    await redis.expire(key, 1, nx=True)
    if n > limit:
        raise RateLimitedError("global email send rate exceeded")


async def _latest_inbound(
    session: AsyncSession, workspace_id: uuid.UUID, conversation_id: uuid.UUID
) -> EmailMessage | None:
    row: EmailMessage | None = await session.scalar(
        select(EmailMessage)
        .where(
            EmailMessage.workspace_id == workspace_id,
            EmailMessage.conversation_id == conversation_id,
            EmailMessage.direction == "in",
        )
        .order_by(EmailMessage.created_at.desc())
        .limit(1)
    )
    return row


async def record_ses_event(*, message_json: str) -> str:
    """Process an SES bounce/complaint notification (config-set event): resolve the workspace from
    the sending address, then suppress the affected recipients. Runs in its own transaction."""
    data = json.loads(message_json)
    ntype = data.get("notificationType") or data.get("eventType")
    mail = data.get("mail", {})

    # Campaign engagement (P1.8): route delivery/open/click/bounce/complaint to the outbound module
    # keyed by the SES MessageId (== sends.provider_id). Non-campaign messages (e.g. agent replies)
    # resolve to a no-op there and are still suppressed below by the source-address path. The lazy
    # import breaks the channels<->outbound service import cycle. Best-effort + isolated: a failure
    # here (transient DB, or 0011 not yet deployed) must NEVER block the compliance-critical
    # bounce/complaint suppression below, so we log-and-continue rather than let it fail the task.
    provider_message_id = mail.get("messageId")
    if provider_message_id and ntype:
        try:
            from relay.modules.outbound import service as outbound_service

            await outbound_service.ingest_ses_engagement(
                provider_message_id=str(provider_message_id),
                ses_event_type=str(ntype),
                sns_message_id=data.get("eventId") or None,
                detail={"notification": ntype},
            )
        except Exception as exc:
            log.warning("channels.ses_event.engagement_failed", error=str(exc))

    _name, source_addr = email.utils.parseaddr(mail.get("source") or "")
    if not source_addr:
        return "no_source"

    if ntype == "Bounce":
        bounce = data.get("bounce", {})
        if bounce.get("bounceType") != "Permanent":
            return "soft_bounce_ignored"
        recipients = [
            r.get("emailAddress")
            for r in bounce.get("bouncedRecipients", [])
            if r.get("emailAddress")
        ]
        reason = "bounce"
    elif ntype == "Complaint":
        complaint = data.get("complaint", {})
        recipients = [
            r.get("emailAddress")
            for r in complaint.get("complainedRecipients", [])
            if r.get("emailAddress")
        ]
        reason = "complaint"
    else:
        return "ignored"

    if not recipients:
        return "no_recipients"

    async with session_scope(None) as session:
        # Status-AGNOSTIC resolver: a bounce/complaint must suppress the recipient even if the
        # sending account is now paused/disabled (deliverability + compliance) — unlike delivery
        # routing, which is active-only.
        row = (
            await session.execute(
                text("SELECT workspace_id FROM channels_resolve_account_workspace(:a)"),
                {"a": source_addr},
            )
        ).first()
        if row is None:
            log.warning("channels.ses_event.unknown_source", source=source_addr)
            return "unknown_source"
        ws_id = uuid.UUID(str(row[0]))
        await set_workspace_guc(session, ws_id)
        for rcpt in recipients:
            await suppress_address(
                session,
                workspace_id=ws_id,
                email=rcpt.lower(),
                reason=reason,
                source="ses",
                detail={"notification": ntype},
            )
    return "suppressed"


async def send_email(
    *, workspace_id: uuid.UUID, conversation_id: uuid.UUID, part_id: uuid.UUID
) -> str:
    """Deliver an agent's email reply. Exactly-once via ``email_messages(workspace_id, part_id)``:
    a prior ``out`` row short-circuits. Read-only checks (suppression / pause / size) raise a clear
    error or return before any write, so their rollback loses nothing. Returns a short status."""
    async with session_scope(workspace_id) as session:
        # Exactly-once gate: was this part already sent?
        already = await session.scalar(
            select(EmailMessage.id).where(
                EmailMessage.workspace_id == workspace_id,
                EmailMessage.part_id == part_id,
                EmailMessage.direction == "out",
            )
        )
        if already is not None:
            return "already_sent"

        part = await messaging_service.get_outbound_part(session, conversation_id, part_id)
        if part is None:
            return "part_not_found"
        if (
            part.channel != "email"
            or part.part_type != "comment"
            or part.author_kind not in ("admin", "ai_agent")
        ):
            return "skip"

        recipient, _name = await crm_service.contact_email(session, part.contact_id)
        if not recipient:
            return "skip_no_recipient"

        # Suppression → clear, permanent error. Record the 'blocked' event in a SEPARATE committed
        # transaction (the outer one rolls back on the raise), then raise so the caller/task sees a
        # clear error and acks without retrying.
        if await is_suppressed(session, workspace_id, recipient):
            async with session_scope(workspace_id) as blocked_session:
                await _record_event(
                    blocked_session,
                    workspace_id=workspace_id,
                    part_id=part_id,
                    email=recipient,
                    event="blocked",
                    detail={"reason": "suppressed"},
                )
            raise SuppressedRecipient("recipient address is suppressed; not sending")

        account = (
            await session.get(ChannelAccount, part.channel_account_id)
            if part.channel_account_id
            else None
        )
        if account is None:
            return "skip_no_account"
        if account.status != "active":
            # Per-tenant send-pause switch (RFC-001 §9): record + ack, do not send.
            await _record_event(
                session,
                workspace_id=workspace_id,
                part_id=part_id,
                email=recipient,
                event="blocked",
                detail={"reason": f"account_{account.status}"},
            )
            return "paused"

        sending_domain = account.address.split("@", 1)[-1] or get_settings().email_inbound_domain
        thread = await _latest_inbound(session, workspace_id, conversation_id)
        # Deterministic Message-ID keyed by part: if a rare crash-after-send-before-commit forces
        # an at-least-once retry (we never drop a reply), the re-sent copy carries the SAME
        # Message-ID so a conformant receiver de-duplicates it. Concurrent duplicates are already
        # impossible via the claim gate below.
        out_message_id = mime.deterministic_message_id(part_id.hex, sending_domain)
        token = reply_token.make_reply_token(workspace_id, conversation_id)
        msg = mime.build_outbound(
            sender=account.address,
            sender_name=get_settings().email_from_name,
            to_addr=recipient,
            reply_to=reply_token.reply_address(token),
            subject=mime.reply_subject(thread.subject if thread else None),
            text_body=part.body or "",
            message_id=out_message_id,
            in_reply_to=thread.message_id if thread else None,
            references=[thread.message_id] if thread and thread.message_id else None,
        )
        raw = mime.render_bytes(msg)
        if len(raw) > get_settings().email_max_message_bytes:
            raise MessageTooLarge("outbound email exceeds the maximum message size")

        await _throttle(workspace_id)  # transient RateLimitedError → task retries

        # EXACTLY-ONCE (review C1): claim the ``(workspace_id, part_id)`` slot BEFORE touching the
        # provider. The unique index serialises concurrent senders — a second worker's INSERT
        # blocks here until we commit (→ it then sees the conflict and skips) or roll back (→ it
        # wins the claim and sends). Only the claim winner calls ``send()``, so the provider is
        # invoked at most once per part; a send failure rolls the claim back so a retry re-sends.
        claimed = (
            await session.execute(
                pg_insert(EmailMessage)
                .values(
                    id=uuid7(),
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                    part_id=part_id,
                    direction="out",
                    message_id=out_message_id,
                    in_reply_to=thread.message_id if thread else None,
                    from_addr=account.address,
                    to_addr=recipient,
                    subject=str(msg["Subject"]),
                )
                .on_conflict_do_nothing(
                    index_elements=[EmailMessage.workspace_id, EmailMessage.part_id]
                )
                .returning(EmailMessage.id)
            )
        ).scalar_one_or_none()
        if claimed is None:
            return "already_sent"  # another sender already owns this part
        await session.flush()

        provider_id = sender.get_sender().send(
            raw=raw, sender=account.address, recipients=[recipient]
        )

        await _record_event(
            session,
            workspace_id=workspace_id,
            part_id=part_id,
            email=recipient,
            event="sent",
            detail={"provider_id": provider_id, "message_id": out_message_id},
        )
        await session.flush()
        return "sent"


async def send_broadcast_email(
    *,
    from_addr: str,
    from_name: str,
    to_addr: str,
    subject: str,
    html_body: str,
    text_body: str,
    message_id: str,
    reply_to: str | None = None,
    list_unsubscribe_url: str | None = None,
) -> str:
    """Transport-only broadcast send for the outbound module (P1.8): build the text+HTML MIME
    (with optional RFC 8058 List-Unsubscribe headers) and hand it to the breaker-wrapped provider.

    Policy (suppression, consent, frequency, exactly-once) lives in ``outbound.service.send_one``;
    this call is a pure send. A transient provider failure (breaker open / SES throttle) is
    surfaced as ``RateLimitedError`` so the outbound send-chunk task retries it (rather than the
    caller swallowing a raw ``SendError`` and silently leaving the recipient unsent); an oversized
    message raises ``MessageTooLarge`` (terminal).
    """
    msg = mime.build_broadcast(
        sender=from_addr,
        sender_name=from_name,
        to_addr=to_addr,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        message_id=message_id,
        reply_to=reply_to,
        list_unsubscribe_url=list_unsubscribe_url,
    )
    raw = mime.render_bytes(msg)
    if len(raw) > get_settings().email_max_message_bytes:
        raise MessageTooLarge("outbound email exceeds the maximum message size")
    try:
        return sender.get_sender().send(raw=raw, sender=from_addr, recipients=[to_addr])
    except sender.SendError as exc:
        # Transient (breaker open / provider throttle) → retryable by the send-chunk task.
        raise RateLimitedError("email provider send failed") from exc
