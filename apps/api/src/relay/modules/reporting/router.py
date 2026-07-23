"""HTTP routes for the ``reporting`` module (P0.9). Mounted by relay.main under ``/v0``.

All endpoints are agent-and-up reads (RBAC enforced in the service layer) and filterable by
``from`` / ``to`` (dates, UTC) and ``team_id``.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Query

from relay.core.deps import CurrentPrincipal, SessionDep

from . import schemas, service

router = APIRouter(tags=["reporting"])

_FROM = Query(default=None, alias="from", description="Start date (UTC, inclusive)")
_TO = Query(default=None, alias="to", description="End date (UTC, inclusive)")


@router.get("/reports/volume", response_model=schemas.VolumeReport)
async def volume(
    principal: CurrentPrincipal,
    session: SessionDep,
    date_from: dt.date | None = _FROM,
    date_to: dt.date | None = _TO,
    team_id: str | None = None,
) -> schemas.VolumeReport:
    return await service.volume(
        session, principal, date_from=date_from, date_to=date_to, team_id=team_id
    )


@router.get("/reports/responsiveness", response_model=schemas.ResponsivenessReport)
async def responsiveness(
    principal: CurrentPrincipal,
    session: SessionDep,
    date_from: dt.date | None = _FROM,
    date_to: dt.date | None = _TO,
    team_id: str | None = None,
) -> schemas.ResponsivenessReport:
    return await service.responsiveness(
        session, principal, date_from=date_from, date_to=date_to, team_id=team_id
    )


@router.get("/reports/csat", response_model=schemas.CsatReport)
async def csat(
    principal: CurrentPrincipal,
    session: SessionDep,
    date_from: dt.date | None = _FROM,
    date_to: dt.date | None = _TO,
    team_id: str | None = None,
) -> schemas.CsatReport:
    return await service.csat(
        session, principal, date_from=date_from, date_to=date_to, team_id=team_id
    )


@router.get("/reports/queue", response_model=schemas.QueueReport)
async def queue(principal: CurrentPrincipal, session: SessionDep) -> schemas.QueueReport:
    return await service.queue(session, principal)
