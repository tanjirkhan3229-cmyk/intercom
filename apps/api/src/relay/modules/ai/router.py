"""HTTP routes for the `ai` module (P1.2). Mounted by relay.main under ``/v0``.

Admin surface only in P1.2: the per-workspace Neko settings (kill switch + grounding gate + scope)
and the run inspector + replay (the "why did Neko say that?" debugging surface, RFC-003 §8). RBAC is
enforced in the service layer (the ``authorize`` choke point); RLS scopes every read to the
workspace. The customer-facing turn is not an HTTP endpoint — it runs on the ai.interactive queue,
triggered by the outbox consumer.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Query

from relay.core.deps import CurrentPrincipal, SessionDep
from relay.core.pagination import Page

from . import schemas, service

router = APIRouter(tags=["ai"])

_FROM = Query(default=None, alias="from", description="Start date (UTC, inclusive)")
_TO = Query(default=None, alias="to", description="End date (UTC, inclusive)")


@router.get("/ai/settings", response_model=schemas.AiSettingsOut)
async def get_ai_settings(
    principal: CurrentPrincipal, session: SessionDep
) -> schemas.AiSettingsOut:
    return await service.get_settings(session, principal)


@router.patch("/ai/settings", response_model=schemas.AiSettingsOut)
async def update_ai_settings(
    req: schemas.AiSettingsUpdate, principal: CurrentPrincipal, session: SessionDep
) -> schemas.AiSettingsOut:
    return await service.update_settings(session, principal, req)


@router.post("/ai/preview", response_model=schemas.SandboxTurnOut)
async def preview_turn(
    req: schemas.SandboxTurnIn, principal: CurrentPrincipal
) -> schemas.SandboxTurnOut:
    """Preview sandbox (admin): run a turn against current knowledge with the retrieval trace
    visible, persisting nothing (RFC-003 §5)."""
    return await service.preview_turn(principal, req)


@router.get("/ai/usage", response_model=schemas.NekoUsageOut)
async def get_neko_usage(principal: CurrentPrincipal, session: SessionDep) -> schemas.NekoUsageOut:
    return await service.neko_usage(session, principal)


@router.get("/ai/runs", response_model=Page[schemas.AgentRunSummary])
async def search_agent_runs(
    principal: CurrentPrincipal,
    session: SessionDep,
    conversation_id: str | None = None,
    outcome: str | None = None,
    q: str | None = Query(default=None, description="Substring match on the customer's question"),
    date_from: dt.date | None = _FROM,
    date_to: dt.date | None = _TO,
    cursor: str | None = None,
    limit: int | None = None,
) -> Page[schemas.AgentRunSummary]:
    """Run inspector search (RFC-003 §8): newest-first, keyset-paginated completed turns, filterable
    by conversation, outcome, question substring, and UTC date range."""
    return await service.search_runs(
        session,
        principal,
        conversation_id=conversation_id,
        outcome=outcome,
        q=q,
        date_from=date_from,
        date_to=date_to,
        cursor=cursor,
        limit=limit,
    )


@router.get("/ai/runs/{run_id}", response_model=schemas.AgentRunDetailOut)
async def get_agent_run(
    run_id: str, principal: CurrentPrincipal, session: SessionDep
) -> schemas.AgentRunDetailOut:
    return await service.get_run(session, principal, run_id)


@router.get("/ai/conversations/{conversation_id}/runs", response_model=list[schemas.AgentRunOut])
async def list_conversation_runs(
    conversation_id: str, principal: CurrentPrincipal, session: SessionDep
) -> list[schemas.AgentRunOut]:
    return await service.list_runs(session, principal, conversation_id)


@router.post("/ai/runs/{run_id}/replay", response_model=schemas.ReplayResult)
async def replay_agent_run(
    run_id: str, principal: CurrentPrincipal, session: SessionDep
) -> schemas.ReplayResult:
    return await service.replay(session, principal, run_id)
