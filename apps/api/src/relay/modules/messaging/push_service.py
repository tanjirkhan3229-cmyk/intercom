"""Device-token registration + push fan-out (P1.10, RFC-000 §2.1).

Intra-module only — the widget router and the ``messaging.send_push`` task call these directly;
cross-module callers go through ``messaging.service``. Registration is a natural-key upsert (so a
rotated token just re-registers); fan-out is at-least-once with a per-``(message, device)`` DB
dedupe gate (master rule 3). Push is best-effort: the message is already durably in the
conversation, so a transient send failure retries a bounded number of times and then gives up.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from relay.core.db import session_scope
from relay.core.ids import IdPrefix, encode_public_id, uuid7
from relay.core.logging import get_logger
from relay.core.principal import ContactPrincipal

from . import push, schemas
from .models import Conversation, ConversationPart, DeviceToken, PushReceipt

log = get_logger(__name__)

_MAX_BODY_PREVIEW = 180


def _device_out(*, device_id: uuid.UUID, platform: str, status: str) -> schemas.DeviceOut:
    return schemas.DeviceOut(
        id=encode_public_id(IdPrefix.DEVICE, device_id), platform=platform, status=status
    )


async def register_device(
    session: AsyncSession, contact: ContactPrincipal, req: schemas.DeviceRegisterIn
) -> schemas.DeviceOut:
    """Upsert the contact's device token. Idempotent by nature: re-registering the same token (a
    rotation, or a background refresh) updates the existing row rather than creating a duplicate."""
    stmt = (
        pg_insert(DeviceToken)
        .values(
            id=uuid7(),
            workspace_id=contact.workspace_id,
            contact_id=contact.contact_id,
            platform=req.platform,
            token=req.token,
            app_id=req.app_id,
            environment=req.environment,
            status="active",
        )
        .on_conflict_do_update(
            constraint="uq_device_tokens_token",
            set_={
                "contact_id": contact.contact_id,
                "platform": req.platform,
                "app_id": req.app_id,
                "environment": req.environment,
                "status": "active",
                "last_seen_at": func.now(),
                "updated_at": func.now(),
            },
        )
        .returning(DeviceToken.id, DeviceToken.platform, DeviceToken.status)
    )
    row = (await session.execute(stmt)).one()
    return _device_out(device_id=row.id, platform=row.platform, status=row.status)


async def unregister_device(
    session: AsyncSession, contact: ContactPrincipal, token: str
) -> None:
    """Deregister a token on logout/uninstall. Scoped to the caller's own devices (defence in
    depth on top of RLS). A no-op if the token is already gone (idempotent)."""
    await session.execute(
        delete(DeviceToken).where(
            DeviceToken.token == token, DeviceToken.contact_id == contact.contact_id
        )
    )


def _preview(body: str | None) -> str:
    text = (body or "").strip()
    if len(text) <= _MAX_BODY_PREVIEW:
        return text
    return text[: _MAX_BODY_PREVIEW - 1].rstrip() + "…"


async def fanout_push_for_part(
    *, workspace_id: uuid.UUID, conversation_id: uuid.UUID, part_id: uuid.UUID
) -> int:
    """Push an agent/AI reply to the conversation's contact's active devices. Returns the number
    of notifications actually sent. Raises ``push.PushSendError`` (transient) to trigger a task
    retry; a dead token marks the device ``stale`` and does not retry."""
    # Read phase: resolve the recipient + eligibility + target devices in one scoped txn.
    async with session_scope(workspace_id) as session:
        conv = await session.get(Conversation, conversation_id)
        if conv is None:
            return 0
        part = (
            await session.execute(
                select(ConversationPart)
                .where(
                    ConversationPart.workspace_id == workspace_id,
                    ConversationPart.id == part_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if part is None:
            return 0
        # Only notify the contact when someone else spoke a real message — never push a contact
        # their own reply, and skip notes/system/assignment parts.
        if part.author_kind not in ("admin", "ai_agent") or part.part_type != "comment":
            return 0
        devices = list(
            (
                await session.execute(
                    select(DeviceToken).where(
                        DeviceToken.contact_id == conv.contact_id,
                        DeviceToken.status == "active",
                    )
                )
            )
            .scalars()
            .all()
        )
        conv_pub = encode_public_id(IdPrefix.CONVERSATION, conv.id)
        title = "New message"
        body = _preview(part.body)

    if not devices:
        return 0

    pusher = push.get_pusher()
    sent = 0
    for device in devices:
        sent += await _deliver_one(
            workspace_id=workspace_id,
            part_id=part_id,
            device_id=device.id,
            platform=device.platform,
            token=device.token,
            app_id=device.app_id,
            environment=device.environment,
            title=title,
            body=body,
            conversation_pub=conv_pub,
            pusher=pusher,
        )
    return sent


async def _deliver_one(
    *,
    workspace_id: uuid.UUID,
    part_id: uuid.UUID,
    device_id: uuid.UUID,
    platform: str,
    token: str,
    app_id: str | None,
    environment: str,
    title: str,
    body: str,
    conversation_pub: str,
    pusher: push.PushDispatcher,
) -> int:
    """Deliver to one device in its own txn so per-device progress survives a retry of the batch.

    Dedupe is a ``push_receipts`` row keyed ``(workspace_id, message_id, device_token_id)``: we
    record it only *after* a successful send (or a terminal dead-token), so a transient failure
    leaves no receipt and the task's retry re-attempts this device. The redelivery window (send
    succeeded, process died before commit) yields at most a duplicate notification — acceptable
    for a best-effort alert.
    """
    async with session_scope(workspace_id) as session:
        already = await session.scalar(
            select(PushReceipt.id).where(
                PushReceipt.message_id == part_id, PushReceipt.device_token_id == device_id
            )
        )
        if already is not None:
            return 0
        msg = push.PushMessage(
            platform=platform,
            token=token,
            title=title,
            body=body,
            topic=app_id,
            environment=environment,
            data={"conversation_id": conversation_pub, "type": "conversation.reply"},
        )
        try:
            provider_id = pusher.send(msg)
        except push.PushTokenInvalid:
            # Terminal for this device: retire the token, record the receipt so it's never retried.
            await session.execute(
                update(DeviceToken).where(DeviceToken.id == device_id).values(status="stale")
            )
            session.add(
                PushReceipt(
                    id=uuid7(),
                    workspace_id=workspace_id,
                    message_id=part_id,
                    device_token_id=device_id,
                    provider_message_id=None,
                )
            )
            log.info("messaging.push.token_stale", device=str(device_id))
            return 0
        session.add(
            PushReceipt(
                id=uuid7(),
                workspace_id=workspace_id,
                message_id=part_id,
                device_token_id=device_id,
                provider_message_id=provider_id,
            )
        )
    return 1
