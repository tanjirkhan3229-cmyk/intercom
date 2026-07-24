"""SLA policies + applied-state clock + durable breach firing (P1.7 — RFC-000 §2.2, RFC-002 §5.6).

Three parts:

1. **Policies** — admin-managed :class:`SlaPolicy` rows (first-response / next-response / resolution
   seconds budgets; optionally business-hours-measured via the S1 office-hours engine).
2. **Applied clock** — :func:`apply_to_conversation` attaches a policy to a conversation, arming
   due-at instants. The clock advances by *reacting to conversation events* off the outbox
   (:func:`apply_conversation_event`, driven by ``sla_consumer``): an agent reply satisfies the
   response targets, a close satisfies resolution, a reopen re-arms resolution (claw-back). Every
   change recomputes ``next_breach_at`` — the min unmet, unbreached due — and ``active``.
3. **Breach firing** — durable and exactly-once. The ``messaging.scan_sla_breaches`` beat task
   calls :func:`sweep_due_breaches`, which claims due rows across workspaces via
   ``relay_claim_due_sla`` (a SECURITY DEFINER ``FOR UPDATE SKIP LOCKED`` + lease claim, mirroring
   workflow ``timers``), marks the passed targets breached, applies escalation, emits
   ``conversation.sla_breached`` and writes ``sla_events``. Re-running the sweep never double-fires
   (a breached target is skipped).

Idempotent folds: the consumer stores ``last_seq`` per row and drops ``seq <= last_seq``; effects
are additionally gated on ``event_time >= applied_at`` so pre-apply history never moves the clock.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from relay.core import outbox, predicates
from relay.core.db import get_engine, session_scope
from relay.core.errors import ConflictError, NotFoundError, ValidationError
from relay.core.ids import IdPrefix, decode_public_id, encode_public_id
from relay.core.logging import get_logger
from relay.core.principal import Principal
from relay.core.rbac import Role, authorize
from relay.modules.identity import service as identity_service

from . import events, office_hours, schemas, service
from .business_hours import add_business_time
from .models import SLA_TARGETS, Conversation, ConversationSla, SlaEvent, SlaPolicy

log = get_logger(__name__)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _decode_or_404(prefix: str, public_id: str, what: str) -> uuid.UUID:
    try:
        return decode_public_id(prefix, public_id)
    except ValueError as exc:
        raise NotFoundError(f"{what} not found") from exc


# --- DTO builders -------------------------------------------------------------


def policy_out(p: SlaPolicy) -> schemas.SlaPolicyOut:
    esc = p.escalation or {}
    return schemas.SlaPolicyOut(
        id=encode_public_id(IdPrefix.SLA_POLICY, p.id),
        name=p.name,
        active=p.active,
        first_response_seconds=p.first_response_seconds,
        next_response_seconds=p.next_response_seconds,
        resolution_seconds=p.resolution_seconds,
        business_hours=p.business_hours,
        apply_predicate=p.apply_predicate,
        escalation=schemas.SlaEscalation(
            set_priority=bool(esc.get("set_priority")),
            notify=bool(esc.get("notify")),
            reassign_team_id=esc.get("reassign_team_id"),
        ),
        position=p.position,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


def _target_state(row: ConversationSla, target: str) -> schemas.SlaTargetState:
    return schemas.SlaTargetState(
        due_at=getattr(row, f"{target}_due_at"),
        satisfied_at=getattr(row, f"{target}_satisfied_at"),
        breached_at=getattr(row, f"{target}_breached_at"),
    )


def conversation_sla_out(row: ConversationSla) -> schemas.ConversationSlaOut:
    return schemas.ConversationSlaOut(
        conversation_id=encode_public_id(IdPrefix.CONVERSATION, row.conversation_id),
        policy_id=encode_public_id(IdPrefix.SLA_POLICY, row.policy_id),
        applied_at=row.applied_at,
        first_response=_target_state(row, "first_response"),
        next_response=_target_state(row, "next_response"),
        resolution=_target_state(row, "resolution"),
        next_breach_at=row.next_breach_at,
        active=row.active,
    )


# --- shared helpers -----------------------------------------------------------


def _enabled_targets(policy: SlaPolicy) -> list[str]:
    return [t for t in SLA_TARGETS if getattr(policy, f"{t}_seconds") is not None]


def _recompute(row: ConversationSla) -> None:
    """Refresh ``next_breach_at`` (min armed-unmet-unbreached due) and ``active``."""
    pending = [
        due
        for t in SLA_TARGETS
        if (due := getattr(row, f"{t}_due_at")) is not None
        and getattr(row, f"{t}_satisfied_at") is None
        and getattr(row, f"{t}_breached_at") is None
    ]
    row.next_breach_at = min(pending) if pending else None
    row.active = row.next_breach_at is not None


async def _due_at(
    session: AsyncSession,
    *,
    business_hours: bool,
    team_id: uuid.UUID | None,
    start: dt.datetime,
    seconds: int | None,
) -> dt.datetime | None:
    """Turn a seconds budget into a due-at, honouring business hours when the policy asks for it and
    a usable schedule exists (else wall-clock)."""
    if seconds is None:
        return None
    if business_hours:
        bh = await office_hours.resolve(session, team_id)
        if bh is not None and bh.weekly_open_seconds > 0:
            return add_business_time(bh, start, seconds)
    return start + dt.timedelta(seconds=seconds)


def _event_time(payload: dict[str, Any]) -> dt.datetime | None:
    raw = payload.get("created_at") or payload.get("occurred_at")
    if not isinstance(raw, str):
        return None
    try:
        return dt.datetime.fromisoformat(raw)
    except ValueError:
        return None


def _team_from_payload(payload: dict[str, Any]) -> uuid.UUID | None:
    tid = payload.get("team_id")
    if isinstance(tid, str):
        try:
            return decode_public_id(IdPrefix.TEAM, tid)
        except ValueError:
            return None
    return None


# --- policy CRUD --------------------------------------------------------------


def _escalation_json(esc: schemas.SlaEscalation) -> dict[str, Any]:
    return {
        "set_priority": esc.set_priority,
        "notify": esc.notify,
        "reassign_team_id": esc.reassign_team_id,
    }


async def _validate_escalation(session: AsyncSession, esc: schemas.SlaEscalation) -> None:
    """Reject an escalation whose ``reassign_team_id`` isn't a team in this workspace — so a foreign
    team id can never be *persisted* (the FK to ``teams`` is RLS-exempt and would otherwise let the
    async breach sweep pin a conversation to another tenant's team). Validated at save time."""
    if esc.reassign_team_id is None:
        return
    try:
        team_id = decode_public_id(IdPrefix.TEAM, esc.reassign_team_id)
    except ValueError as exc:
        raise ValidationError("invalid reassign_team_id") from exc
    if not await identity_service.team_exists(session, team_id):
        raise ValidationError("reassign_team_id is not a team in this workspace")


async def create_policy(
    session: AsyncSession, principal: Principal, req: schemas.SlaPolicyIn
) -> schemas.SlaPolicyOut:
    authorize(principal, min_role=Role.ADMIN)
    if req.apply_predicate is not None:
        predicates.validate_predicate(req.apply_predicate)  # 422 on a malformed AST
    await _validate_escalation(session, req.escalation)
    policy = SlaPolicy(
        workspace_id=principal.workspace_id,
        name=req.name,
        active=req.active,
        first_response_seconds=req.first_response_seconds,
        next_response_seconds=req.next_response_seconds,
        resolution_seconds=req.resolution_seconds,
        business_hours=req.business_hours,
        apply_predicate=req.apply_predicate,
        escalation=_escalation_json(req.escalation),
        position=req.position,
        created_by=principal.admin_id,
    )
    session.add(policy)
    await session.flush()
    return policy_out(policy)


async def update_policy(
    session: AsyncSession, principal: Principal, public_id: str, req: schemas.SlaPolicyIn
) -> schemas.SlaPolicyOut:
    authorize(principal, min_role=Role.ADMIN)
    pid = _decode_or_404(IdPrefix.SLA_POLICY, public_id, "sla policy")
    policy = await session.get(SlaPolicy, pid)
    if policy is None:
        raise NotFoundError("sla policy not found")
    if req.apply_predicate is not None:
        predicates.validate_predicate(req.apply_predicate)
    await _validate_escalation(session, req.escalation)
    policy.name = req.name
    policy.active = req.active
    policy.first_response_seconds = req.first_response_seconds
    policy.next_response_seconds = req.next_response_seconds
    policy.resolution_seconds = req.resolution_seconds
    policy.business_hours = req.business_hours
    policy.apply_predicate = req.apply_predicate
    policy.escalation = _escalation_json(req.escalation)
    policy.position = req.position
    # Set explicitly (not via the server-side ``onupdate``): an ORM-expired server default would
    # trigger a sync lazy-reload in ``policy_out`` and raise MissingGreenlet on the async driver.
    policy.updated_at = _now()
    await session.flush()
    return policy_out(policy)


async def list_policies(session: AsyncSession) -> list[schemas.SlaPolicyOut]:
    rows = (
        await session.scalars(select(SlaPolicy).order_by(SlaPolicy.position, SlaPolicy.created_at))
    ).all()
    return [policy_out(p) for p in rows]


async def get_policy(session: AsyncSession, public_id: str) -> schemas.SlaPolicyOut:
    pid = _decode_or_404(IdPrefix.SLA_POLICY, public_id, "sla policy")
    policy = await session.get(SlaPolicy, pid)
    if policy is None:
        raise NotFoundError("sla policy not found")
    return policy_out(policy)


async def delete_policy(session: AsyncSession, principal: Principal, public_id: str) -> None:
    authorize(principal, min_role=Role.ADMIN)
    pid = _decode_or_404(IdPrefix.SLA_POLICY, public_id, "sla policy")
    policy = await session.get(SlaPolicy, pid)
    if policy is None:
        raise NotFoundError("sla policy not found")
    await session.delete(policy)  # ON DELETE CASCADE removes any conversation_sla rows
    await session.flush()


# --- apply / remove / read ----------------------------------------------------


async def _apply_core(
    session: AsyncSession, conv: Conversation, policy: SlaPolicy, *, now: dt.datetime
) -> ConversationSla:
    """Attach ``policy`` to ``conv`` (upsert; re-apply resets the clock). Writes ``sla_events``
    (applied) for each enabled target. No outbox event — applying isn't a thread event."""
    fr_due = await _due_at(
        session,
        business_hours=policy.business_hours,
        team_id=conv.team_id,
        start=now,
        seconds=policy.first_response_seconds,
    )
    res_due = await _due_at(
        session,
        business_hours=policy.business_hours,
        team_id=conv.team_id,
        start=now,
        seconds=policy.resolution_seconds,
    )
    pending = [d for d in (fr_due, res_due) if d is not None]
    next_breach = min(pending) if pending else None

    reset: dict[str, Any] = {
        "policy_id": policy.id,
        "applied_at": now,
        "first_response_due_at": fr_due,
        "first_response_satisfied_at": None,
        "first_response_breached_at": None,
        "next_response_due_at": None,
        "next_response_satisfied_at": None,
        "next_response_breached_at": None,
        "resolution_due_at": res_due,
        "resolution_satisfied_at": None,
        "resolution_breached_at": None,
        "next_breach_at": next_breach,
        "active": next_breach is not None,
        # NB: ``last_seq`` is deliberately absent — a fresh insert gets the server-default 0, and a
        # re-apply KEEPS the existing watermark so already-consumed events aren't re-folded during a
        # consumer PEL replay (the applied_at gate alone wouldn't drop a post-apply-time replay).
        "claimed_by": None,
        "claimed_at": None,
        "updated_at": now,
    }
    stmt = (
        pg_insert(ConversationSla)
        .values(workspace_id=conv.workspace_id, conversation_id=conv.id, **reset)
        .on_conflict_do_update(constraint="uq_conversation_sla_conv", set_=reset)
    )
    await session.execute(stmt)
    row = (
        await session.execute(
            select(ConversationSla).where(ConversationSla.conversation_id == conv.id)
        )
    ).scalar_one()
    for target in _enabled_targets(policy):
        session.add(
            SlaEvent(
                workspace_id=conv.workspace_id,
                conversation_id=conv.id,
                policy_id=policy.id,
                target=target,
                kind="applied",
                occurred_at=now,
            )
        )
    return row


async def _load_active_policy(session: AsyncSession, policy_id: uuid.UUID) -> SlaPolicy:
    policy = await session.get(SlaPolicy, policy_id)
    if policy is None:
        raise NotFoundError("sla policy not found")
    if not policy.active:
        raise ConflictError("sla policy is not active")
    return policy


async def apply_sla(
    session: AsyncSession, principal: Principal, conversation_public: str, req: schemas.ApplySlaIn
) -> schemas.ConversationSlaOut:
    """Manually attach an SLA policy to a conversation (an inbox action; agent+)."""
    authorize(principal, min_role=Role.AGENT)
    conv = await service.load_for_update_public(session, conversation_public)
    policy = await _load_active_policy(
        session, _decode_or_404(IdPrefix.SLA_POLICY, req.policy_id, "sla policy")
    )
    row = await _apply_core(session, conv, policy, now=_now())
    return conversation_sla_out(row)


async def system_apply_sla(
    session: AsyncSession, *, conversation_id: uuid.UUID, policy_id: uuid.UUID
) -> None:
    """Attach an SLA policy from the workflow ``apply_sla`` action (no acting admin). A missing/
    inactive policy or foreign conversation raises, which the executor records as a skipped step."""
    conv = await service.load_for_update(session, conversation_id)
    policy = await _load_active_policy(session, policy_id)
    await _apply_core(session, conv, policy, now=_now())


async def remove_sla(session: AsyncSession, principal: Principal, conversation_public: str) -> None:
    """Stop tracking SLAs on a conversation (delete the applied row). Agent+."""
    authorize(principal, min_role=Role.AGENT)
    cid = _decode_or_404(IdPrefix.CONVERSATION, conversation_public, "conversation")
    row = (
        await session.execute(select(ConversationSla).where(ConversationSla.conversation_id == cid))
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("no SLA applied to this conversation")
    await session.delete(row)
    await session.flush()


async def get_conversation_sla(
    session: AsyncSession, conversation_public: str
) -> schemas.ConversationSlaOut:
    cid = _decode_or_404(IdPrefix.CONVERSATION, conversation_public, "conversation")
    row = (
        await session.execute(select(ConversationSla).where(ConversationSla.conversation_id == cid))
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("no SLA applied to this conversation")
    return conversation_sla_out(row)


# --- rule auto-apply (on conversation.created) --------------------------------


def _conversation_context(conv: Conversation) -> dict[str, Any]:
    """Flat predicate context for SLA apply rules (mirrors the fields custom views filter on)."""
    return {
        "channel": conv.channel,
        "state": conv.state,
        "priority": conv.priority,
        "team_id": encode_public_id(IdPrefix.TEAM, conv.team_id) if conv.team_id else None,
        "assignee_id": (
            encode_public_id(IdPrefix.ADMIN, conv.assignee_id) if conv.assignee_id else None
        ),
        "attributes": conv.attributes or {},
    }


async def maybe_auto_apply(session: AsyncSession, conversation_id: uuid.UUID) -> bool:
    """Apply the first matching active rule-policy (by ``position``) to a new conversation, unless
    one is already applied. Returns whether a policy was applied."""
    already = await session.scalar(
        select(ConversationSla.id).where(ConversationSla.conversation_id == conversation_id)
    )
    if already is not None:
        return False
    policies = (
        await session.scalars(
            select(SlaPolicy)
            .where(SlaPolicy.active.is_(True), SlaPolicy.apply_predicate.isnot(None))
            .order_by(SlaPolicy.position, SlaPolicy.created_at)
        )
    ).all()
    if not policies:
        return False
    conv = await session.get(Conversation, conversation_id)
    if conv is None:
        return False
    ctx = _conversation_context(conv)
    for policy in policies:
        if policy.apply_predicate is not None and predicates.evaluate(policy.apply_predicate, ctx):
            await _apply_core(session, conv, policy, now=_now())
            return True
    return False


# --- event fold (driven by sla_consumer) --------------------------------------


def _write_met(session: AsyncSession, row: ConversationSla, target: str, at: dt.datetime) -> None:
    session.add(
        SlaEvent(
            workspace_id=row.workspace_id,
            conversation_id=row.conversation_id,
            policy_id=row.policy_id,
            target=target,
            kind="met",
            occurred_at=at,
        )
    )


def _satisfy(session: AsyncSession, row: ConversationSla, target: str, at: dt.datetime) -> None:
    """Satisfy ``target`` iff armed, unmet and unbreached (a late reply never un-breaches)."""
    if (
        getattr(row, f"{target}_due_at") is not None
        and getattr(row, f"{target}_satisfied_at") is None
        and getattr(row, f"{target}_breached_at") is None
    ):
        setattr(row, f"{target}_satisfied_at", at)
        _write_met(session, row, target, at)


def _target_resolved(row: ConversationSla, target: str) -> bool:
    return (
        getattr(row, f"{target}_due_at") is None
        or getattr(row, f"{target}_satisfied_at") is not None
        or getattr(row, f"{target}_breached_at") is not None
    )


async def apply_conversation_event(
    session: AsyncSession,
    row: ConversationSla,
    policy: SlaPolicy,
    topic: str,
    payload: dict[str, Any],
) -> None:
    """Advance the applied clock for one conversation event. Effects only apply at/after
    ``applied_at``; the caller owns ``last_seq`` idempotency and the row lock."""
    at = _event_time(payload)
    if at is None or at < row.applied_at:
        return

    if topic == events.CONVERSATION_PART_CREATED and payload.get("part_type") == "comment":
        author = payload.get("author_kind")
        if author == "contact":
            # A contact message arms next-response, but only (a) once the first-response phase is
            # over (before that, first-response is the governing clock) and (b) when next-response
            # isn't already armed — otherwise a burst of follow-up messages would keep pushing the
            # deadline forward (gaming the SLA) or clear an already-recorded breach. The deadline is
            # anchored to the *first* unanswered message and only re-arms after the agent replies
            # (satisfied) or it breaches — a fresh obligation.
            if (
                policy.next_response_seconds is not None
                and _target_resolved(row, "first_response")
                and _target_resolved(row, "next_response")
            ):
                row.next_response_due_at = await _due_at(
                    session,
                    business_hours=policy.business_hours,
                    team_id=_team_from_payload(payload),
                    start=at,
                    seconds=policy.next_response_seconds,
                )
                row.next_response_satisfied_at = None
                row.next_response_breached_at = None
        elif author in ("admin", "ai_agent"):
            _satisfy(session, row, "first_response", at)
            _satisfy(session, row, "next_response", at)
    elif topic == events.CONVERSATION_STATE_CHANGED:
        to_state = payload.get("to")
        if to_state == "closed":
            _satisfy(session, row, "resolution", at)
        elif to_state == "open" and policy.resolution_seconds is not None:
            # Reopen ⇒ re-arm resolution (claw back a prior breach/satisfaction).
            row.resolution_due_at = await _due_at(
                session,
                business_hours=policy.business_hours,
                team_id=_team_from_payload(payload),
                start=at,
                seconds=policy.resolution_seconds,
            )
            row.resolution_satisfied_at = None
            row.resolution_breached_at = None
    _recompute(row)


# --- durable breach firing (beat sweep) ---------------------------------------


async def _process_breach(
    session: AsyncSession, sla_id: uuid.UUID, conversation_id: uuid.UUID
) -> int:
    """Fire the breaches for one claimed row. Idempotent: an already-breached target is skipped, so
    re-running the sweep (crash / lease reclaim / overlapping beat) never double-fires the same
    target. Returns the number of targets breached now.

    Locks the conversation head **before** the SLA row — the same order as the apply path
    (conv → sla) — so a concurrent ``apply_sla`` and a sweep on one conversation can't deadlock.
    """
    try:
        conv = await service.load_for_update(session, conversation_id)
    except NotFoundError:
        return 0  # conversation deleted out from under the claim (CASCADE); nothing to do
    row = (
        await session.execute(
            select(ConversationSla).where(ConversationSla.id == sla_id).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        return 0
    now = _now()
    breached = [
        t
        for t in SLA_TARGETS
        if (due := getattr(row, f"{t}_due_at")) is not None
        and getattr(row, f"{t}_satisfied_at") is None
        and getattr(row, f"{t}_breached_at") is None
        and due <= now
    ]
    row.claimed_by = None
    row.claimed_at = None
    if not breached:
        _recompute(row)
        return 0
    for t in breached:
        setattr(row, f"{t}_breached_at", now)

    policy = await session.get(SlaPolicy, row.policy_id)
    await _apply_escalation(session, conv, policy, breached)

    for t in breached:
        session.add(
            SlaEvent(
                workspace_id=row.workspace_id,
                conversation_id=conv.id,
                policy_id=row.policy_id,
                target=t,
                kind="breached",
                occurred_at=now,
            )
        )
    await outbox.emit(
        session,
        aggregate=events.AGGREGATE_CONVERSATION,
        aggregate_id=conv.id,
        topic=events.CONVERSATION_SLA_BREACHED,
        payload={
            **service.conversation_payload(conv),
            "policy_id": encode_public_id(IdPrefix.SLA_POLICY, row.policy_id),
            "targets": breached,
        },
    )
    _recompute(row)
    return len(breached)


async def _apply_escalation(
    session: AsyncSession, conv: Conversation, policy: SlaPolicy | None, breached: list[str]
) -> None:
    esc: dict[str, Any] = (policy.escalation if policy else None) or {}
    if esc.get("set_priority"):
        conv.priority = True
    reassign = esc.get("reassign_team_id")
    if isinstance(reassign, str):
        try:
            conv.team_id = decode_public_id(IdPrefix.TEAM, reassign)
            conv.assignee_id = None
            await service.record_system_assignment(session, conv)
        except ValueError:
            log.warning("sla.escalation.bad_team", team=reassign)
    if esc.get("notify"):
        await service.append_system_part(
            session,
            conv,
            part_type="note",
            body=f"SLA breached: {', '.join(breached)}",
            meta={"sla_breach": breached},
        )


async def sweep_due_breaches(max_rows: int = 500, lease_seconds: int = 120) -> int:
    """Claim + fire all due SLA breaches across every workspace. Returns targets breached.

    Claiming runs the ``relay_claim_due_sla`` SECURITY DEFINER function on a connection with **no**
    ``app.ws`` (it bypasses RLS to see all tenants); each claimed row is then processed under its
    own workspace RLS scope.
    """
    async with get_engine().begin() as conn:
        rows = (
            await conn.execute(
                sa.text(
                    "SELECT workspace_id, id, conversation_id FROM relay_claim_due_sla(:m, :l)"
                ),
                {"m": max_rows, "l": lease_seconds},
            )
        ).all()
    if not rows:
        return 0
    # Process each claimed row in its OWN transaction: a breach is durable the instant it fires
    # (bounding the lease-reclaim window) and the conversation/SLA row locks are held only for that
    # one breach, never across the whole batch.
    total = 0
    for ws_id, sla_id, conv_id in rows:
        async with session_scope(ws_id) as session:
            total += await _process_breach(session, sla_id, conv_id)
    if total:
        log.info("sla.sweep.breached", targets=total)
    return total
