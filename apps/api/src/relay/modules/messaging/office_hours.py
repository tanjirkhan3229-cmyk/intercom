"""Office-hours schedule service (P1.7 — RFC-000 §2.2).

CRUD over :class:`~relay.modules.messaging.models.OfficeHoursSchedule` plus :func:`resolve`, which
returns the effective :class:`~relay.modules.messaging.business_hours.BusinessHours` for a
conversation (a team override falling back to the workspace default). The SLA subsystem (S2) and
the widget's expected-reply-time both consume :func:`resolve`; writes are admin-only via the
``authorize`` choke point. RLS scopes every read/write to the caller's workspace.
"""

from __future__ import annotations

import datetime as dt
import uuid

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from relay.core.errors import NotFoundError, ValidationError
from relay.core.ids import IdPrefix, encode_public_id, uuid7
from relay.core.principal import Principal
from relay.core.rbac import Role, authorize

from . import schemas
from .business_hours import BusinessHours, build_business_hours, is_open
from .models import OfficeHoursSchedule


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _decode_team_or_404(public_id: str) -> uuid.UUID:
    try:
        from relay.core.ids import decode_public_id

        return decode_public_id(IdPrefix.TEAM, public_id)
    except ValueError as exc:
        raise NotFoundError("team not found") from exc


def _weekly_to_json(weekly: dict[str, list[schemas.OfficeHoursInterval]]) -> dict[str, object]:
    return {
        day: [{"open": iv.open, "close": iv.close} for iv in ivs] for day, ivs in weekly.items()
    }


def _out(row: OfficeHoursSchedule) -> schemas.OfficeHoursScheduleOut:
    weekly: dict[str, list[schemas.OfficeHoursInterval]] = {}
    for day, ivs in (row.weekly or {}).items():
        weekly[day] = [
            schemas.OfficeHoursInterval(open=iv["open"], close=iv["close"]) for iv in ivs
        ]
    return schemas.OfficeHoursScheduleOut(
        id=encode_public_id(IdPrefix.OFFICE_HOURS_SCHEDULE, row.id),
        team_id=encode_public_id(IdPrefix.TEAM, row.team_id) if row.team_id else None,
        timezone=row.timezone,
        weekly=weekly,
        holidays=list(row.holidays or []),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def to_business_hours(row: OfficeHoursSchedule) -> BusinessHours:
    """Parse a stored row into the pure engine's :class:`BusinessHours` (validated at write time,
    so this never raises for a persisted row)."""
    return build_business_hours(row.timezone, row.weekly, row.holidays)


async def upsert_schedule(
    session: AsyncSession, principal: Principal, req: schemas.OfficeHoursScheduleIn
) -> schemas.OfficeHoursScheduleOut:
    """Create or replace the schedule for a team (or the workspace default). Admin-only.

    The payload is validated by building a :class:`BusinessHours` first, so a bad timezone /
    interval / holiday is a 422 *before* any write. Upsert is keyed on the
    ``uq_office_hours_ws_team`` constraint (NULLS NOT DISTINCT for the default row).
    """
    authorize(principal, min_role=Role.ADMIN)
    weekly_json = _weekly_to_json(req.weekly)
    build_business_hours(req.timezone, weekly_json, req.holidays)  # 422 on bad input

    team_id = _decode_team_or_404(req.team_id) if req.team_id is not None else None
    now = _now()
    insert = (
        pg_insert(OfficeHoursSchedule)
        .values(
            id=uuid7(),
            workspace_id=principal.workspace_id,
            team_id=team_id,
            timezone=req.timezone,
            weekly=weekly_json,
            holidays=list(req.holidays),
            updated_at=now,
        )
        .on_conflict_do_update(
            constraint="uq_office_hours_ws_team",
            set_={
                "timezone": req.timezone,
                "weekly": weekly_json,
                "holidays": list(req.holidays),
                "updated_at": now,
            },
        )
    )
    try:
        await session.execute(insert)
    except sa.exc.IntegrityError as exc:  # unknown team_id (FK)
        raise ValidationError("unknown team") from exc

    row = (
        await session.execute(
            select(OfficeHoursSchedule).where(
                OfficeHoursSchedule.team_id == team_id
                if team_id is not None
                else OfficeHoursSchedule.team_id.is_(None)
            )
        )
    ).scalar_one()
    return _out(row)


async def list_schedules(session: AsyncSession) -> list[schemas.OfficeHoursScheduleOut]:
    """All schedules in the workspace (default first, then per team). RLS-scoped."""
    rows = (
        await session.scalars(
            select(OfficeHoursSchedule).order_by(
                OfficeHoursSchedule.team_id.is_(None).desc(), OfficeHoursSchedule.created_at
            )
        )
    ).all()
    return [_out(r) for r in rows]


async def delete_schedule(session: AsyncSession, principal: Principal, public_id: str) -> None:
    """Delete a schedule by public id. Admin-only. 404 if it isn't the workspace's."""
    authorize(principal, min_role=Role.ADMIN)
    try:
        from relay.core.ids import decode_public_id

        sid = decode_public_id(IdPrefix.OFFICE_HOURS_SCHEDULE, public_id)
    except ValueError as exc:
        raise NotFoundError("office-hours schedule not found") from exc
    row = await session.get(OfficeHoursSchedule, sid)
    if row is None:
        raise NotFoundError("office-hours schedule not found")
    await session.delete(row)
    await session.flush()


async def _fetch(session: AsyncSession, team_id: uuid.UUID | None) -> OfficeHoursSchedule | None:
    where = (
        OfficeHoursSchedule.team_id == team_id
        if team_id is not None
        else OfficeHoursSchedule.team_id.is_(None)
    )
    return (await session.execute(select(OfficeHoursSchedule).where(where))).scalar_one_or_none()


async def resolve(session: AsyncSession, team_id: uuid.UUID | None) -> BusinessHours | None:
    """Effective business hours for a conversation: the team's schedule, else the workspace
    default, else ``None`` (no schedule configured → callers treat SLAs as 24/7). RLS-scoped."""
    if team_id is not None:
        team_row = await _fetch(session, team_id)
        if team_row is not None:
            return to_business_hours(team_row)
    default_row = await _fetch(session, None)
    return to_business_hours(default_row) if default_row is not None else None


async def status(
    session: AsyncSession, principal: Principal, team_public_id: str | None
) -> schemas.OfficeHoursStatusOut:
    """Whether the effective schedule is open right now (powers the widget expected-reply-time)."""
    authorize(principal, min_role=Role.AGENT)
    team_id = _decode_team_or_404(team_public_id) if team_public_id is not None else None
    bh = await resolve(session, team_id)
    if bh is None:
        return schemas.OfficeHoursStatusOut(has_schedule=False, is_open=True, timezone=None)
    return schemas.OfficeHoursStatusOut(
        has_schedule=True, is_open=is_open(bh, _now()), timezone=str(bh.tz)
    )
