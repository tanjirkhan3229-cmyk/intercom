"""Service interface for the `ai` module (RFC-003) — the ONLY surface other modules may import.

Three responsibilities:

- **Turn orchestration** — :func:`run_turn` is a thin re-export of the pipeline (RFC-003 §3); the
  Celery task and tests call it here so the module boundary stays clean.
- **Settings** — the per-workspace Neko kill switch + grounding gate + channel scope + persona
  (RFC-003 §5-6). Admin-only writes.
- **Debuggability** — the run inspector + a **replay tool** (RFC-003 §8): every production turn
  reconstructs from its ``agent_runs`` row. Replay re-runs generation from the stored ``trace`` and
  checks the prompt hash + answer reproduce — the "why did Neko say that?" guarantee.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from relay.core.errors import NotFoundError, ValidationError
from relay.core.ids import IdPrefix, decode_public_id, encode_public_id, uuid7
from relay.core.pagination import Page, clamp_limit
from relay.core.principal import Principal
from relay.core.rbac import Role, authorize
from relay.modules.ai import ledger, metering, prompts, schemas
from relay.modules.ai.metering import (
    confirm_resolution,
    on_conversation_reopened,
    resolve_by_silence,
)
from relay.modules.ai.models import AgentRun, AiSettings
from relay.modules.ai.pipeline import TurnResult, run_turn, sandbox_run
from relay.modules.ai.prompts import EvidenceChunk
from relay.modules.ai.resilience import StreamOutcome, build_router

# Cross-module entry points other modules call: turn orchestration + the RFC-003 §8 metering hooks
# (messaging invokes ``confirm_resolution``/``on_conversation_reopened``; the beat task invokes
# ``resolve_by_silence``). Re-exported here so callers only ever import ``ai.service``.
__all__ = [
    "TurnResult",
    "confirm_resolution",
    "on_conversation_reopened",
    "resolve_by_silence",
    "run_turn",
]

# The chunk source kinds a workspace may scope Neko to (mirrors RFC-002 §5.5 content_chunks).
_ALLOWED_SOURCE_KINDS = frozenset({"article", "pdf", "url", "snippet", "custom_answer"})
_ALLOWED_TONES = frozenset({"friendly", "neutral", "formal"})
_ALLOWED_OFFICE_HOURS = frozenset({"answer", "handoff"})


def _decode_or_404(prefix: str, public_id: str, what: str) -> uuid.UUID:
    try:
        return decode_public_id(prefix, public_id)
    except ValueError as exc:
        raise NotFoundError(f"{what} not found") from exc


# --- Settings -----------------------------------------------------------------


def _settings_out_from_view(view: ledger.AiSettingsView) -> schemas.AiSettingsOut:
    return schemas.AiSettingsOut(
        enabled=view.enabled,
        channels=view.channels,
        grounding_threshold=view.grounding_threshold,
        max_clarifications=view.max_clarifications,
        source_kinds=view.source_kinds,
        tone=view.tone,
        persona=view.persona,
        answer_max_tokens=view.answer_max_tokens,
        always_handoff_intents=view.always_handoff_intents,
        office_hours_behavior=view.office_hours_behavior,
        monthly_spend_cap_usd=(
            float(view.monthly_spend_cap_usd) if view.monthly_spend_cap_usd is not None else None
        ),
    )


async def _get_settings_row(session: AsyncSession, workspace_id: uuid.UUID) -> AiSettings | None:
    row: AiSettings | None = await session.scalar(
        select(AiSettings).where(AiSettings.workspace_id == workspace_id)
    )
    return row


async def get_settings(session: AsyncSession, principal: Principal) -> schemas.AiSettingsOut:
    """Read the workspace's Neko settings (any teammate). Returns defaults (Neko off) when unset."""
    authorize(principal, min_role=Role.AGENT)
    view = await ledger.load_settings(session, principal.workspace_id)
    return _settings_out_from_view(view)


async def update_settings(
    session: AsyncSession, principal: Principal, req: schemas.AiSettingsUpdate
) -> schemas.AiSettingsOut:
    """Upsert the workspace's Neko settings (admin only) — the per-workspace kill switch + gate."""
    authorize(principal, min_role=Role.ADMIN)
    if req.source_kinds is not None:
        bad = [k for k in req.source_kinds if k not in _ALLOWED_SOURCE_KINDS]
        if bad:
            raise ValidationError(f"unknown source_kinds: {bad}")
    if req.tone is not None and req.tone not in _ALLOWED_TONES:
        raise ValidationError(f"unknown tone: {req.tone!r} (expected {sorted(_ALLOWED_TONES)})")
    if (
        req.office_hours_behavior is not None
        and req.office_hours_behavior not in _ALLOWED_OFFICE_HOURS
    ):
        raise ValidationError(f"unknown office_hours_behavior: {req.office_hours_behavior!r}")
    provided: dict[str, Any] = {
        f: getattr(req, f)
        for f in (
            "enabled",
            "channels",
            "grounding_threshold",
            "max_clarifications",
            "source_kinds",
            "tone",
            "persona",
            "answer_max_tokens",
            "always_handoff_intents",
            "office_hours_behavior",
            "monthly_spend_cap_usd",
        )
        if getattr(req, f) is not None
    }
    stmt = pg_insert(AiSettings).values(id=uuid7(), workspace_id=principal.workspace_id, **provided)
    if provided:
        stmt = stmt.on_conflict_do_update(index_elements=[AiSettings.workspace_id], set_=provided)
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=[AiSettings.workspace_id])
    await session.execute(stmt)
    row = await _get_settings_row(session, principal.workspace_id)
    assert row is not None  # just upserted this workspace's row
    return _settings_out_from_view(ledger.view_from_row(row))


# --- Preview sandbox (RFC-003 §5, P1.3) ---------------------------------------


async def preview_turn(principal: Principal, req: schemas.SandboxTurnIn) -> schemas.SandboxTurnOut:
    """Run a turn against the workspace's current knowledge and return the answer + full retrieval
    trace, persisting nothing (admins see *why* an answer happened). The trace shape matches an
    ``agent_runs`` row (P1.3 acceptance)."""
    authorize(principal, min_role=Role.ADMIN)
    record = await sandbox_run(
        workspace_id=principal.workspace_id,
        message=req.message,
        history=[(m.role, m.body) for m in req.history],
    )
    return schemas.SandboxTurnOut(
        outcome=record.outcome,
        handoff_reason=record.handoff_reason,
        rewritten_query=record.rewritten_query,
        retrieved=record.retrieved,
        grounding_score=record.grounding_score,
        citations=record.citations,
        verdict=record.verdict,
        answer=record.answer,
        prompt_hash=record.prompt_hash,
        provider=record.provider,
        models=record.models,
        tokens=record.tokens,
        cost_usd=record.cost_usd,
        latency_ms=record.latency_ms,
        trace=record.trace,
    )


# --- Usage / spend cap (RFC-003 §9) -------------------------------------------


async def neko_usage(session: AsyncSession, principal: Principal) -> schemas.NekoUsageOut:
    """Month-to-date Neko resolutions + spend for the workspace, and whether it's over its cap."""
    authorize(principal, min_role=Role.AGENT)
    view = await ledger.load_settings(session, principal.workspace_id)
    summary = await metering.usage_summary(
        session, principal.workspace_id, view.monthly_spend_cap_usd
    )
    return schemas.NekoUsageOut(
        month_resolutions=float(summary.resolutions),
        month_spend_usd=float(summary.spend_usd),
        monthly_spend_cap_usd=float(summary.cap_usd) if summary.cap_usd is not None else None,
        over_cap=summary.over_cap,
    )


# --- Run inspector (RFC-003 §8) -----------------------------------------------


def _run_out(run: AgentRun) -> schemas.AgentRunOut:
    return schemas.AgentRunOut(
        id=encode_public_id(IdPrefix.AGENT_RUN, run.id),
        conversation_id=encode_public_id(IdPrefix.CONVERSATION, run.conversation_id),
        status=run.status,
        outcome=run.outcome,
        handoff_reason=run.handoff_reason,
        language=run.language,
        safety_class=run.safety_class,
        query=run.query,
        rewritten_query=run.rewritten_query,
        retrieved=run.retrieved,
        grounding_score=run.grounding_score,
        prompt_hash=run.prompt_hash,
        provider=run.provider,
        models=run.models,
        answer=run.answer,
        citations=run.citations,
        verdict=run.verdict,
        tokens=run.tokens,
        cost_usd=run.cost_usd,
        latency_ms=run.latency_ms,
        created_at=run.created_at,
    )


def _run_detail(run: AgentRun) -> schemas.AgentRunDetailOut:
    """The full run incl. the replayable ``trace`` (retrieved evidence content) — the inspector's
    "why did Neko say X" payload (P1.4)."""
    return schemas.AgentRunDetailOut(**_run_out(run).model_dump(), trace=run.trace)


def _run_summary(run: AgentRun) -> schemas.AgentRunSummary:
    return schemas.AgentRunSummary(
        id=encode_public_id(IdPrefix.AGENT_RUN, run.id),
        conversation_id=encode_public_id(IdPrefix.CONVERSATION, run.conversation_id),
        created_at=run.created_at,
        status=run.status,
        outcome=run.outcome,
        handoff_reason=run.handoff_reason,
        grounding_score=run.grounding_score,
        cost_usd=run.cost_usd,
        latency_total_ms=run.latency_ms.get("total") if isinstance(run.latency_ms, dict) else None,
        query=run.query,
    )


async def get_run(
    session: AsyncSession, principal: Principal, run_public_id: str
) -> schemas.AgentRunDetailOut:
    authorize(principal, min_role=Role.AGENT)
    run = await ledger.get_run(session, _decode_or_404(IdPrefix.AGENT_RUN, run_public_id, "run"))
    if run is None:  # RLS-scoped: another tenant's id is a clean 404
        raise NotFoundError("run not found")
    return _run_detail(run)


async def list_runs(
    session: AsyncSession, principal: Principal, conversation_public_id: str, *, limit: int = 50
) -> list[schemas.AgentRunOut]:
    authorize(principal, min_role=Role.AGENT)
    cid = _decode_or_404(IdPrefix.CONVERSATION, conversation_public_id, "conversation")
    return [_run_out(r) for r in await ledger.list_runs(session, cid, limit=limit)]


def _day_bounds(
    date_from: dt.date | None, date_to: dt.date | None
) -> tuple[dt.datetime | None, dt.datetime | None]:
    """UTC half-open ``[from 00:00, to+1 00:00)`` datetimes for a run's ``created_at`` filter."""
    frm = dt.datetime.combine(date_from, dt.time.min, tzinfo=dt.UTC) if date_from else None
    to = (
        dt.datetime.combine(date_to + dt.timedelta(days=1), dt.time.min, tzinfo=dt.UTC)
        if date_to
        else None
    )
    return frm, to


async def search_runs(
    session: AsyncSession,
    principal: Principal,
    *,
    conversation_id: str | None = None,
    outcome: str | None = None,
    q: str | None = None,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    cursor: str | None = None,
    limit: int | None = None,
) -> Page[schemas.AgentRunSummary]:
    """Run-inspector search (P1.4, RFC-003 §8): completed turns across the workspace, newest-first,
    keyset-paginated. Filterable by conversation, outcome, a question substring, and a UTC date
    range. RLS scopes to the caller's workspace, so a bad conversation/cursor id yields an empty
    page rather than leaking another tenant's runs."""
    authorize(principal, min_role=Role.AGENT)
    n = clamp_limit(limit)
    cid = (
        _decode_or_404(IdPrefix.CONVERSATION, conversation_id, "conversation")
        if conversation_id
        else None
    )
    cursor_id = _decode_or_404(IdPrefix.AGENT_RUN, cursor, "cursor") if cursor else None
    created_from, created_to = _day_bounds(date_from, date_to)

    rows = await ledger.search_runs(
        session,
        conversation_id=cid,
        outcome=outcome,
        query_text=q,
        created_from=created_from,
        created_to=created_to,
        cursor_id=cursor_id,
        limit=n,
    )
    next_cursor = None
    if len(rows) > n:
        rows = rows[:n]
        next_cursor = encode_public_id(IdPrefix.AGENT_RUN, rows[-1].id)
    return Page(items=[_run_summary(r) for r in rows], next_cursor=next_cursor)


# --- Replay (RFC-003 §8 — "reconstructs from agent_runs") ---------------------


async def replay(
    session: AsyncSession, principal: Principal, run_public_id: str
) -> schemas.ReplayResult:
    """Re-run a stored turn's generation from its ``trace`` and check it reproduces (RFC-003 §8).

    The **prompt hash always reproduces** (same inputs ⇒ same prompt); the **answer reproduces
    exactly** under a deterministic/seeded model (the CI provider), which is what makes a production
    turn debuggable. Turns that never reached generation (ineligible / instant handoff / low
    grounding) are trivially reproducible — there is nothing to regenerate."""
    authorize(principal, min_role=Role.AGENT)
    run = await ledger.get_run(session, _decode_or_404(IdPrefix.AGENT_RUN, run_public_id, "run"))
    if run is None:
        raise NotFoundError("run not found")

    run_pub = encode_public_id(IdPrefix.AGENT_RUN, run.id)
    if not run.prompt_hash:
        return schemas.ReplayResult(
            run_id=run_pub,
            reproducible=True,
            prompt_hash_match=True,
            answer_match=True,
            original_prompt_hash=None,
            replay_prompt_hash=None,
            original_answer=None,
            replay_answer=None,
        )

    trace = run.trace
    evidence = [
        EvidenceChunk(
            label=e["label"],
            chunk_id=uuid.UUID(e["chunk_id"]),
            source_id=uuid.UUID(e["source_id"]),
            source_kind=e["source_kind"],
            content=e["content"],
            title=e.get("title"),
            heading_path=e.get("heading_path"),
            score=e.get("score", 0.0),
        )
        for e in trace.get("evidence", [])
    ]
    gen_msgs = prompts.generation_messages(
        workspace_name=trace["workspace_name"],
        customer_text=trace["customer_text"],
        chunks=evidence,
        persona=trace.get("persona"),
        history_summary=trace.get("history_summary"),
    )
    replay_hash = prompts.prompt_hash(gen_msgs)

    router = build_router()
    outcome = StreamOutcome()
    buf: list[str] = []
    async for chunk in router.stream(
        tier="frontier",
        messages=gen_msgs,
        outcome=outcome,
        max_tokens=int(trace.get("answer_max_tokens", 400)),
    ):
        if chunk.delta:
            buf.append(chunk.delta)
    replay_raw = "".join(buf)
    original_raw = trace.get("raw_answer")

    hash_match = replay_hash == run.prompt_hash
    answer_match = replay_raw == original_raw
    return schemas.ReplayResult(
        run_id=run_pub,
        reproducible=hash_match and answer_match,
        prompt_hash_match=hash_match,
        answer_match=answer_match,
        original_prompt_hash=run.prompt_hash,
        replay_prompt_hash=replay_hash,
        original_answer=original_raw,
        replay_answer=replay_raw,
    )
