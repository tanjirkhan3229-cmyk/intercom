"""Resolution metering — RFC-003 §8, the billing-grade resolution definition (implemented verbatim).

A conversation counts as a Neko resolution **iff**:

1. Neko *participated* (it answered at least once — ``ai_status`` is ``active``/``resolved``); and
2. *no human teammate replied* after Neko's last answer; and
3. the customer *confirmed* resolution **or** went *silent for 72 h* after the answer; and
4. the conversation was *not reopened within 72 h* — a reopen **claws back** the meter.

The +1 meter is written in the SAME transaction as the qualifying state change (master rule 2):
the confirm path (customer action) and the silence path (a beat task) both mark the conversation
``resolved`` + close it + record one ``usage_records`` unit atomically. Corrections are appended
negative rows, never mutations (RFC-002 §5.6 W8): a reopen inside the 72 h window appends a ``-1``.

This module owns the *policy*; ``messaging.service`` owns the conversation state + part facts
(:func:`~relay.modules.messaging.service.resolution_facts`), and ``billing.service`` owns the
generic meter (``record_usage`` / ``clawback_resolution``) — billing never learns what a
"resolution" is. The meter's ``source_id`` is the closing ``state_change`` part id, so a
resolve→reopen→re-resolve cycle meters once per cycle and the claw-back targets the right unit.

Spend cap (RFC-003 §9): the pipeline consults :func:`is_over_spend_cap` in preflight; past the cap
Neko routes to a human (never a silent drop) and admins are notified once per month
(:func:`notify_spend_cap_reached`).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from relay.core import outbox
from relay.core.ids import IdPrefix, encode_public_id
from relay.core.logging import get_logger
from relay.core.redis import get_redis
from relay.modules.ai import events
from relay.modules.ai.models import AgentRun
from relay.modules.billing import service as billing_service
from relay.modules.messaging import service as messaging_service

log = get_logger(__name__)

# RFC-003 §8 windows (hours). Per-tenant configurability "within bounds" is a documented follow-up
# (RFC-003 §10) — ponytail: two constants until a tenant actually asks to move them.
SILENCE_HOURS = 72
CLAWBACK_HOURS = 72

# ``ai_status`` values that mean "Neko participated" (RFC-003 §8 clause 1). ``handed_off`` (a human
# took over) and ``None`` (Neko never touched it) do not qualify.
_PARTICIPATED = frozenset({"active", "resolved"})

# TTL on the "admins already notified this month" Redis marker: comfortably longer than a month so
# the once-per-month guarantee holds across the whole billing period.
_CAP_NOTIFY_TTL_SECONDS = 40 * 24 * 60 * 60


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _qualifies(facts: messaging_service.ResolutionFacts) -> bool:
    """RFC-003 §8 clauses 1-2: Neko participated and no human replied after its last answer."""
    return (
        facts.ai_status in _PARTICIPATED
        and facts.last_neko_answer_at is not None
        and not facts.human_replied_after_neko
    )


async def _last_run_answered(session: AsyncSession, conversation_id: uuid.UUID) -> bool:
    """Whether Neko's most recent completed turn actually *answered* (vs clarified/handed off). The
    silence path uses this to stay conservative — an unanswered clarifying question that the
    customer ghosts is not a billable resolution (the confirm path needs no such guard: an explicit
    customer confirmation is intent enough)."""
    outcome = await session.scalar(
        select(AgentRun.outcome)
        .where(AgentRun.conversation_id == conversation_id, AgentRun.status == "complete")
        .order_by(AgentRun.id.desc())
        .limit(1)
    )
    return outcome == "answered"


async def _resolve(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    kind: str,
    require_answered: bool,
) -> bool:
    """Mark a conversation resolved by Neko and meter it, if it qualifies (RFC-003 §8). Returns
    True iff a resolution unit was metered. Idempotent: an already-closed conversation is a no-op
    (``close_for_resolution`` returns None), so a redelivered confirm / silence sweep never
    double-meters."""
    facts = await messaging_service.resolution_facts(session, conversation_id)
    if facts is None or facts.state == "closed" or not _qualifies(facts):
        return False
    if require_answered and not await _last_run_answered(session, conversation_id):
        return False
    await messaging_service.set_ai_status(
        session, conversation_id=conversation_id, status="resolved"
    )
    close_part_id = await messaging_service.close_for_resolution(
        session, conversation_id, reason=f"neko_{kind}"
    )
    if close_part_id is None:  # became closed under us — nothing to meter
        return False
    metered = await billing_service.record_usage(
        session,
        workspace_id=workspace_id,
        meter=billing_service.RESOLUTION_METER,
        qty=1,
        source_id=str(close_part_id),
    )
    if metered:
        log.info(
            "neko.resolution.metered",
            workspace_id=str(workspace_id),
            conversation_id=str(conversation_id),
            kind=kind,
        )
    return metered


async def confirm_resolution(
    session: AsyncSession, *, workspace_id: uuid.UUID, conversation_id: uuid.UUID
) -> bool:
    """The customer confirmed Neko resolved their question (RFC-003 §8 "confirmed resolution")."""
    return await _resolve(
        session, workspace_id, conversation_id, kind="confirm", require_answered=False
    )


async def resolve_by_silence(
    session: AsyncSession, *, workspace_id: uuid.UUID, conversation_id: uuid.UUID
) -> bool:
    """72 h of customer silence after Neko's answer (RFC-003 §8) — driven by the beat sweep."""
    return await _resolve(
        session, workspace_id, conversation_id, kind="silence", require_answered=True
    )


async def on_conversation_reopened(
    session: AsyncSession, *, workspace_id: uuid.UUID, conversation_id: uuid.UUID
) -> None:
    """A conversation reopened — claw back its Neko resolution meter if the resolve was metered and
    the reopen is inside the 72 h window (RFC-003 §8). Runs in the reopen txn (master rule 2). A
    reopen *after* the window leaves the resolution standing (a new cycle begins). No-op if the
    close was never a metered Neko resolution (``clawback_resolution`` checks that)."""
    facts = await messaging_service.resolution_facts(session, conversation_id)
    if facts is None or facts.last_close_part_id is None or facts.last_close_at is None:
        return
    if _now() - facts.last_close_at > dt.timedelta(hours=CLAWBACK_HOURS):
        return  # reopened after the window — the resolution stands
    clawed = await billing_service.clawback_resolution(
        session, workspace_id=workspace_id, close_source_id=str(facts.last_close_part_id)
    )
    if clawed:
        log.info(
            "neko.resolution.clawed_back",
            workspace_id=str(workspace_id),
            conversation_id=str(conversation_id),
        )


# --- Spend cap (RFC-003 §9) ---------------------------------------------------


@dataclass(frozen=True)
class NekoUsage:
    resolutions: Decimal
    spend_usd: Decimal
    cap_usd: Decimal | None
    over_cap: bool


async def usage_summary(
    session: AsyncSession, workspace_id: uuid.UUID, cap_usd: Decimal | None
) -> NekoUsage:
    """Month-to-date resolutions + spend for a workspace, and whether it's over its cap."""
    usage = await billing_service.resolution_usage_this_month(session, workspace_id)
    over = cap_usd is not None and usage.spend_usd >= cap_usd
    return NekoUsage(
        resolutions=usage.resolutions, spend_usd=usage.spend_usd, cap_usd=cap_usd, over_cap=over
    )


async def is_over_spend_cap(
    session: AsyncSession, workspace_id: uuid.UUID, cap_usd: Decimal | None
) -> bool:
    """True iff month-to-date Neko spend has reached the workspace's cap (RFC-003 §9). No cap ⇒
    never over."""
    if cap_usd is None:
        return False
    return (await usage_summary(session, workspace_id, cap_usd)).over_cap


async def notify_spend_cap_reached(
    session: AsyncSession, workspace_id: uuid.UUID, *, now: dt.datetime | None = None
) -> None:
    """Emit the admin-notification signal for a cap breach, at most once per workspace per month.

    Deduped on a Redis ``SET NX`` marker keyed by workspace + month, so the per-turn cap check
    doesn't spam admins. The durable signal is an ``ai.neko.spend_cap_reached`` outbox event
    (consumed downstream by email/webhooks — RFC-001 §6.5); the emit rides the caller's txn."""
    now = now or _now()
    marker = f"neko:capnotify:{workspace_id}:{now:%Y%m}"
    first = await get_redis().set(marker, "1", nx=True, ex=_CAP_NOTIFY_TTL_SECONDS)
    if not first:
        return  # already notified this month
    usage = await billing_service.resolution_usage_this_month(session, workspace_id, now=now)
    await outbox.emit(
        session,
        aggregate=events.AGGREGATE_WORKSPACE,
        aggregate_id=workspace_id,
        topic=events.SPEND_CAP_REACHED,
        payload={
            "workspace_id": encode_public_id(IdPrefix.WORKSPACE, workspace_id),
            "month": f"{now:%Y-%m}",
            "resolutions": str(usage.resolutions),
            "spend_usd": str(usage.spend_usd),
        },
    )
    log.warning(
        "neko.spend_cap.reached",
        workspace_id=str(workspace_id),
        spend_usd=str(usage.spend_usd),
    )
