"""Balanced (load-aware) assignment + agent availability (P1.7 S4).

:func:`assign_balanced` routes an unassigned conversation to the **least-loaded eligible agent**
of a team: an assignable team member (``identity_service.team_agent_ids``) who is not ``away``
and below their ``max_open`` cap. "Load" is the agent's current open-conversation count read
authoritatively from the ``conversations`` head, so a sequential burst distributes evenly (±1); the
final placement is an atomic ``UPDATE … WHERE assignee_id IS NULL`` claim, so a concurrent claim can
never double-assign. (A Redis load cache is the documented scale-time optimisation; correctness here
comes from the DB count + the atomic claim.)

Availability (``away`` / ``max_open``) is managed by the agent for themselves or by an admin for any
teammate. RLS scopes every read/write to the workspace.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from relay.core.errors import ConflictError, NotFoundError, ValidationError
from relay.core.ids import IdPrefix, decode_public_id, encode_public_id
from relay.core.principal import Principal
from relay.core.rbac import Role, authorize
from relay.modules.identity import service as identity_service

from . import schemas, service
from .models import AgentAvailability, Conversation


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _decode_or_404(prefix: str, public_id: str, what: str) -> uuid.UUID:
    try:
        return decode_public_id(prefix, public_id)
    except ValueError as exc:
        raise NotFoundError(f"{what} not found") from exc


def _availability_out(
    admin_id: uuid.UUID, row: AgentAvailability | None
) -> schemas.AgentAvailabilityOut:
    return schemas.AgentAvailabilityOut(
        admin_id=encode_public_id(IdPrefix.ADMIN, admin_id),
        away=row.away if row is not None else False,
        max_open=row.max_open if row is not None else None,
        updated_at=row.updated_at if row is not None else _now(),
    )


async def _upsert(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    admin_id: uuid.UUID,
    req: schemas.AgentAvailabilityIn,
) -> AgentAvailability:
    now = _now()
    stmt = (
        pg_insert(AgentAvailability)
        .values(
            workspace_id=workspace_id,
            admin_id=admin_id,
            away=req.away,
            max_open=req.max_open,
            updated_at=now,
        )
        .on_conflict_do_update(
            constraint="uq_agent_availability_admin",
            set_={"away": req.away, "max_open": req.max_open, "updated_at": now},
        )
    )
    await session.execute(stmt)
    return (
        await session.execute(
            select(AgentAvailability).where(AgentAvailability.admin_id == admin_id)
        )
    ).scalar_one()


async def get_my_availability(
    session: AsyncSession, principal: Principal
) -> schemas.AgentAvailabilityOut:
    authorize(principal, min_role=Role.AGENT)
    row = (
        await session.execute(
            select(AgentAvailability).where(AgentAvailability.admin_id == principal.admin_id)
        )
    ).scalar_one_or_none()
    return _availability_out(principal.admin_id, row)


async def set_my_availability(
    session: AsyncSession, principal: Principal, req: schemas.AgentAvailabilityIn
) -> schemas.AgentAvailabilityOut:
    authorize(principal, min_role=Role.AGENT)
    row = await _upsert(session, principal.workspace_id, principal.admin_id, req)
    return _availability_out(principal.admin_id, row)


async def set_availability(
    session: AsyncSession, principal: Principal, admin_public: str, req: schemas.AgentAvailabilityIn
) -> schemas.AgentAvailabilityOut:
    """Admin override of a teammate's availability."""
    authorize(principal, min_role=Role.ADMIN)
    admin_id = _decode_or_404(IdPrefix.ADMIN, admin_public, "admin")
    # The row is workspace-scoped (RLS) and only consulted for actual team members, so an id that
    # isn't a member is inert; no separate membership check is needed.
    row = await _upsert(session, principal.workspace_id, admin_id, req)
    return _availability_out(admin_id, row)


async def list_availability(session: AsyncSession) -> list[schemas.AgentAvailabilityOut]:
    rows = (
        await session.scalars(select(AgentAvailability).order_by(AgentAvailability.admin_id))
    ).all()
    return [_availability_out(r.admin_id, r) for r in rows]


async def _pick_least_loaded(session: AsyncSession, agents: list[uuid.UUID]) -> uuid.UUID:
    """The least-loaded assignable agent, skipping ``away`` / at-capacity. Deterministic tiebreak by
    ``admin_id`` so a burst distributes evenly and reproducibly."""
    avail = {
        r.admin_id: r
        for r in (
            await session.scalars(
                select(AgentAvailability).where(AgentAvailability.admin_id.in_(agents))
            )
        ).all()
    }
    load_rows = (
        await session.execute(
            select(Conversation.assignee_id, func.count())
            .where(Conversation.state == "open", Conversation.assignee_id.in_(agents))
            .group_by(Conversation.assignee_id)
        )
    ).all()
    counts = {aid: int(n) for aid, n in load_rows}

    best: tuple[int, uuid.UUID] | None = None
    for admin_id in agents:
        row = avail.get(admin_id)
        if row is not None and row.away:
            continue
        load = counts.get(admin_id, 0)
        if row is not None and row.max_open is not None and load >= row.max_open:
            continue
        candidate = (load, admin_id)
        if best is None or candidate < best:
            best = candidate
    if best is None:
        raise ConflictError("all assignable agents in the team are away or at capacity")
    return best[1]


async def assign_balanced(
    session: AsyncSession,
    principal: Principal,
    conversation_public: str,
    req: schemas.BalancedAssignIn,
) -> schemas.ConversationOut:
    """Assign an unassigned conversation to the least-loaded eligible agent of a team (S4)."""
    authorize(principal, min_role=Role.AGENT)
    cid = _decode_or_404(IdPrefix.CONVERSATION, conversation_public, "conversation")
    team_id = _decode_or_404(IdPrefix.TEAM, req.team_id, "team")
    agents = await identity_service.team_agent_ids(session, team_id)
    if not agents:
        raise ValidationError("team has no assignable agents")
    chosen = await _pick_least_loaded(session, agents)

    claimed = (
        await session.execute(
            update(Conversation)
            .where(Conversation.id == cid, Conversation.assignee_id.is_(None))
            .values(assignee_id=chosen, team_id=team_id)
            .returning(Conversation.id)
        )
    ).scalar_one_or_none()
    conv = await service.load_for_update(session, cid)
    if claimed is not None:
        await service.record_assignment(session, principal, conv)
    return service.conversation_out(conv)
