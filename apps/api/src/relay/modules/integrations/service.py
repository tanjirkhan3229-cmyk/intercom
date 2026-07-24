"""Service layer for the ``integrations`` module (P1.9): Slack + Zapier.

Cross-module reuse (allowed: ``modules.* -> modules.*.service``/``.events``): Zapier REST-hook
triggers create real ``webhook_subscriptions`` via ``webhooks.service`` (reusing the whole
delivery/retry/signing pipeline), and reply-from-Slack posts through ``messaging.service``. Slack
secrets are Fernet-encrypted at rest (core/crypto); the outbound call goes through the SSRF-guarded
client (core/ssrf).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import functools
import json
import uuid
from typing import Any

import httpx
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from relay.core.crypto import InvalidToken, decrypt_secret, encrypt_secret
from relay.core.db import session_scope
from relay.core.errors import ConflictError, NotFoundError, ValidationError
from relay.core.ids import IdPrefix, decode_public_id, encode_public_id
from relay.core.logging import get_logger
from relay.core.principal import Principal
from relay.core.rbac import Role, authorize
from relay.core.redis import get_redis
from relay.core.ssrf import SsrfError, guarded_post, validate_target
from relay.modules.messaging import service as messaging_service
from relay.modules.webhooks import events as webhook_events
from relay.modules.webhooks import service as webhooks_service
from relay.settings import get_settings

from . import schemas
from .models import IntegrationAccount, SlackThreadMap

log = get_logger(__name__)


class SlackDeliveryError(Exception):
    """A Slack post failed (transport, non-2xx, or ``ok=false``) — the notify task retries."""


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def integration_out(a: IntegrationAccount) -> schemas.IntegrationOut:
    cfg = a.config
    return schemas.IntegrationOut(
        id=encode_public_id(IdPrefix.INTEGRATION_ACCOUNT, a.id),
        integration_type=a.integration_type,
        status=a.status,
        team_id=cfg.get("team_id"),
        team_name=cfg.get("team_name"),
        channel_id=cfg.get("channel_id"),
        channel_name=cfg.get("channel_name"),
        created_at=a.created_at,
    )


def _decode_or_404(prefix: str, public_id: str, what: str) -> uuid.UUID:
    try:
        return decode_public_id(prefix, public_id)
    except ValueError as exc:
        raise NotFoundError(f"{what} not found") from exc


# --- Slack integration CRUD (ADMIN) -------------------------------------------


async def connect_slack(
    session: AsyncSession, principal: Principal, req: schemas.SlackConnect
) -> schemas.IntegrationOut:
    """Connect a Slack workspace (ADMIN). Bot token + signing secret are encrypted at rest."""
    authorize(principal, min_role=Role.ADMIN)
    account = IntegrationAccount(
        workspace_id=principal.workspace_id,
        integration_type="slack",
        status="active",
        config={
            "team_id": req.team_id,
            "team_name": req.team_name,
            "channel_id": req.channel_id,
            "channel_name": req.channel_name,
            "bot_token_ciphertext": encrypt_secret(req.bot_token),
            "signing_secret_ciphertext": encrypt_secret(req.signing_secret),
        },
        created_by=principal.admin_id,
    )
    session.add(account)
    try:
        await session.flush()
    except Exception as exc:  # global-unique active team_id (one Slack workspace ↔ one Relay ws)
        if "uq_integration_accounts_slack_team" in str(exc):
            raise ConflictError("this Slack workspace is already connected") from exc
        raise
    return integration_out(account)


async def list_integrations(session: AsyncSession) -> list[schemas.IntegrationOut]:
    rows = (
        await session.scalars(select(IntegrationAccount).order_by(IntegrationAccount.id.desc()))
    ).all()
    return [integration_out(a) for a in rows]


async def _get_account(session: AsyncSession, account_id: uuid.UUID) -> IntegrationAccount:
    account = await session.get(IntegrationAccount, account_id)
    if account is None:
        raise NotFoundError("integration not found")
    return account


async def get_integration(session: AsyncSession, public_id: str) -> schemas.IntegrationOut:
    aid = _decode_or_404(IdPrefix.INTEGRATION_ACCOUNT, public_id, "integration")
    return integration_out(await _get_account(session, aid))


async def set_integration_status(
    session: AsyncSession,
    principal: Principal,
    public_id: str,
    req: schemas.IntegrationStatusUpdate,
) -> schemas.IntegrationOut:
    authorize(principal, min_role=Role.ADMIN)
    aid = _decode_or_404(IdPrefix.INTEGRATION_ACCOUNT, public_id, "integration")
    account = await _get_account(session, aid)
    account.status = req.status
    account.updated_at = _now()
    try:
        await session.flush()
    except Exception as exc:  # re-activating collides with another ws already active on this team
        if "uq_integration_accounts_slack_team" in str(exc):
            raise ConflictError("this Slack workspace is already connected elsewhere") from exc
        raise
    return integration_out(account)


async def delete_integration(session: AsyncSession, principal: Principal, public_id: str) -> None:
    authorize(principal, min_role=Role.ADMIN)
    aid = _decode_or_404(IdPrefix.INTEGRATION_ACCOUNT, public_id, "integration")
    account = await _get_account(session, aid)
    await session.delete(account)  # slack_thread_map cascades
    await session.flush()


async def has_active_slack(session: AsyncSession) -> bool:
    """True if the (RLS-scoped) workspace has ≥1 active Slack integration — the consumer's gate."""
    row = await session.scalar(
        select(IntegrationAccount.id)
        .where(
            IntegrationAccount.integration_type == "slack", IntegrationAccount.status == "active"
        )
        .limit(1)
    )
    return row is not None


# --- Slack inbound: resolve workspace by team (unauthenticated callback) -------


async def resolve_slack_account_by_team(team_id: str) -> tuple[uuid.UUID, str] | None:
    """Return ``(workspace_id, signing_secret_ciphertext)`` for the active Slack account of a Slack
    team, or None. Runs via a SECURITY DEFINER helper because the inbound callback is unauthed
    (no ``app.ws``); the global-unique active team_id guarantees at most one row."""
    async with session_scope(None) as session:
        row = (
            await session.execute(
                text(
                    "SELECT workspace_id, signing_secret_ciphertext "
                    "FROM relay_slack_account_by_team(:t)"
                ),
                {"t": team_id},
            )
        ).one_or_none()
    if row is None:
        return None
    return uuid.UUID(str(row[0])), str(row[1])


# --- Slack outbound: post a notification (worker) -----------------------------


def _post_to_slack(token: str, channel: str, tstext: str, thread_ts: str | None) -> str | None:
    """POST chat.postMessage through the SSRF-guarded client. Returns the message ``ts`` on success;
    raises :class:`SlackDeliveryError` on transport error, non-2xx, or a Slack ``ok=false``."""
    settings = get_settings()
    body: dict[str, Any] = {"channel": channel, "text": tstext}
    if thread_ts is not None:
        body["thread_ts"] = thread_ts
    try:
        resp = guarded_post(
            f"{settings.slack_api_base_url}/chat.postMessage",
            content=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {token}",
            },
            timeout=settings.slack_delivery_timeout_seconds,
            allow_private=settings.slack_allow_private_targets,
        )
    except (httpx.HTTPError, SsrfError) as exc:
        # SsrfError also covers a transient DNS-resolution failure of the Slack host → retry.
        raise SlackDeliveryError(f"transport: {type(exc).__name__}") from exc
    if not (200 <= resp.status_code < 300):
        raise SlackDeliveryError(f"HTTP {resp.status_code}")
    data = resp.json()
    if not data.get("ok"):
        raise SlackDeliveryError(f"slack: {data.get('error', 'unknown')}")
    ts = data.get("ts")
    return str(ts) if ts is not None else None


async def _snapshot_slack_plan(
    session: AsyncSession, conversation_id: uuid.UUID
) -> list[tuple[uuid.UUID, str, str, str | None]]:
    """For each active Slack account: ``(account_id, channel, bot_token, existing_thread_ts)``."""
    accounts = (
        await session.scalars(
            select(IntegrationAccount).where(
                IntegrationAccount.integration_type == "slack",
                IntegrationAccount.status == "active",
            )
        )
    ).all()
    plan: list[tuple[uuid.UUID, str, str, str | None]] = []
    for acc in accounts:
        channel = acc.config.get("channel_id")
        if not channel:
            continue
        try:
            token = decrypt_secret(acc.config["bot_token_ciphertext"])
        except (KeyError, InvalidToken):
            log.error("integrations.slack.bad_token", account=str(acc.id))
            continue
        existing = await session.scalar(
            select(SlackThreadMap.thread_ts).where(
                SlackThreadMap.integration_account_id == acc.id,
                SlackThreadMap.conversation_id == conversation_id,
            )
        )
        plan.append((acc.id, channel, token, existing))
    return plan


async def deliver_slack_notification(
    workspace_id: uuid.UUID, conversation_pub: str, topic: str, text_body: str
) -> str:
    """Post ``text_body`` to every active Slack account's channel, threading under the
    conversation's existing root message (or starting one). Raises :class:`SlackDeliveryError` so
    the task retries a transient failure.

    Starting a thread is serialised by a short per-conversation Redis lock: the two events emitted
    when a conversation opens (``conversation.created`` + the first contact ``part.created``) would
    otherwise race and post two root messages, orphaning one thread for inbound replies. A task that
    can't take the lock retries — by then the thread exists and it threads under it.
    """
    conversation_id = decode_public_id(IdPrefix.CONVERSATION, conversation_pub)
    redis = get_redis()
    lock_key = f"slk:threadlock:{workspace_id}:{conversation_id}"

    async with session_scope(workspace_id) as session:
        plan = await _snapshot_slack_plan(session, conversation_id)

    lock_held = False
    if any(thread_ts is None for _a, _c, _tok, thread_ts in plan):
        lock_held = bool(await redis.set(lock_key, "1", nx=True, ex=30))
        if not lock_held:
            raise SlackDeliveryError("thread creation in progress; retrying")
        # Re-snapshot under the lock: a concurrent task may have created the thread just now.
        async with session_scope(workspace_id) as session:
            plan = await _snapshot_slack_plan(session, conversation_id)

    try:
        posted: list[tuple[uuid.UUID, str, str]] = []  # (account, channel, root_ts) for new threads
        for account_id, channel, token, thread_ts in plan:
            ts = _post_to_slack(token, channel, text_body, thread_ts)
            if thread_ts is None and ts is not None:
                posted.append((account_id, channel, ts))
        if posted:
            async with session_scope(workspace_id) as session:
                for account_id, channel, root_ts in posted:
                    await session.execute(
                        pg_insert(SlackThreadMap)
                        .values(
                            workspace_id=workspace_id,
                            integration_account_id=account_id,
                            conversation_id=conversation_id,
                            channel_id=channel,
                            thread_ts=root_ts,
                        )
                        .on_conflict_do_nothing()
                    )
    finally:
        if lock_held:
            await redis.delete(lock_key)
    return "delivered"


# --- Slack inbound: ingest a signed event (worker) ----------------------------


async def ingest_slack_event(workspace_id: uuid.UUID, event_json: str) -> str:
    """Turn a Slack thread reply into a Relay admin reply. Ignores bot/edited/system messages (loop
    guard), dedupes on Slack's ``event_id``, and maps ``(channel, thread_ts)`` to a conversation.
    """
    data = json.loads(event_json)
    event = data.get("event", {})
    if event.get("type") != "message" or event.get("bot_id") or event.get("subtype"):
        return "ignored"
    channel = event.get("channel")
    thread_ts = event.get("thread_ts")
    body = event.get("text")
    event_id = data.get("event_id")
    if not (channel and thread_ts and body and event_id):
        return "ignored"

    # Claim the event id (prevents a concurrent redelivery double-posting). On a DB failure we
    # RELEASE the claim and re-raise so the task's retry can reprocess — the claim must never
    # outlive a failed write, or the customer's reply would be lost forever.
    redis = get_redis()
    marker = f"slk:inbound:{event_id}"
    if not await redis.set(marker, "1", nx=True, ex=3600):
        return "duplicate"
    try:
        async with session_scope(workspace_id) as session:
            tmap = (
                await session.scalars(
                    select(SlackThreadMap).where(
                        SlackThreadMap.channel_id == channel,
                        SlackThreadMap.thread_ts == thread_ts,
                    )
                )
            ).one_or_none()
            if tmap is None:
                return "no_mapping"  # unmappable → keep the claim (don't reprocess forever)
            await messaging_service.system_add_admin_reply(
                session,
                conversation_id=tmap.conversation_id,
                body=body,
                author_id=None,
                source="slack",
            )
    except Exception:
        await redis.delete(marker)  # release the claim so a retry can reprocess
        raise
    return "posted"


# --- Zapier (REST hooks reuse the webhooks pipeline) --------------------------


def zapier_auth_test(principal: Principal) -> schemas.ZapierAuthTestOut:
    """Validate a Zapier connection: any authenticated API-key principal passes."""
    return schemas.ZapierAuthTestOut(
        ok=True, workspace_id=encode_public_id(IdPrefix.WORKSPACE, principal.workspace_id)
    )


async def zapier_subscribe(
    session: AsyncSession, principal: Principal, req: schemas.ZapierSubscribe
) -> schemas.ZapierSubscribeOut:
    """Create a REST-hook subscription (a real webhook_subscription) for a Zapier trigger."""
    if req.topic not in webhook_events.WEBHOOK_TOPICS:
        raise ValidationError(
            "unknown topic",
            details={"topic": req.topic, "allowed": sorted(webhook_events.WEBHOOK_TOPICS)},
        )
    await asyncio.get_running_loop().run_in_executor(
        None,
        functools.partial(
            validate_target,
            req.target_url,
            allow_private=get_settings().webhook_allow_private_targets,
        ),
    )
    sub = await webhooks_service.system_create_subscription(
        session,
        workspace_id=principal.workspace_id,
        url=req.target_url,
        topics=[req.topic],
        created_by=principal.admin_id,
    )
    return schemas.ZapierSubscribeOut(
        id=encode_public_id(IdPrefix.WEBHOOK_SUBSCRIPTION, sub.id),
        topic=req.topic,
        target_url=req.target_url,
    )


async def zapier_unsubscribe(session: AsyncSession, public_id: str) -> None:
    sub_id = _decode_or_404(IdPrefix.WEBHOOK_SUBSCRIPTION, public_id, "subscription")
    deleted = await webhooks_service.system_delete_subscription(session, sub_id)
    if not deleted:
        raise NotFoundError("subscription not found")
