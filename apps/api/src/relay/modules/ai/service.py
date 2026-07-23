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

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from relay.core.errors import NotFoundError, ValidationError
from relay.core.ids import IdPrefix, decode_public_id, encode_public_id, uuid7
from relay.core.principal import Principal
from relay.core.rbac import Role, authorize
from relay.modules.ai import ledger, prompts, schemas
from relay.modules.ai.models import AgentRun, AiSettings
from relay.modules.ai.pipeline import TurnResult, run_turn
from relay.modules.ai.prompts import EvidenceChunk
from relay.modules.ai.resilience import StreamOutcome, build_router

__all__ = ["TurnResult", "run_turn"]

# The chunk source kinds a workspace may scope Neko to (mirrors RFC-002 §5.5 content_chunks).
_ALLOWED_SOURCE_KINDS = frozenset({"article", "pdf", "url", "snippet", "custom_answer"})


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
        persona=view.persona,
        answer_max_tokens=view.answer_max_tokens,
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
    provided: dict[str, Any] = {
        f: getattr(req, f)
        for f in (
            "enabled",
            "channels",
            "grounding_threshold",
            "max_clarifications",
            "source_kinds",
            "persona",
            "answer_max_tokens",
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
    return _settings_out_from_view(
        ledger.AiSettingsView(
            enabled=row.enabled,
            channels=list(row.channels),
            grounding_threshold=row.grounding_threshold,
            max_clarifications=row.max_clarifications,
            source_kinds=list(row.source_kinds) if row.source_kinds is not None else None,
            persona=row.persona,
            answer_max_tokens=row.answer_max_tokens,
        )
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


async def get_run(
    session: AsyncSession, principal: Principal, run_public_id: str
) -> schemas.AgentRunOut:
    authorize(principal, min_role=Role.AGENT)
    run = await ledger.get_run(session, _decode_or_404(IdPrefix.AGENT_RUN, run_public_id, "run"))
    if run is None:  # RLS-scoped: another tenant's id is a clean 404
        raise NotFoundError("run not found")
    return _run_out(run)


async def list_runs(
    session: AsyncSession, principal: Principal, conversation_public_id: str, *, limit: int = 50
) -> list[schemas.AgentRunOut]:
    authorize(principal, min_role=Role.AGENT)
    cid = _decode_or_404(IdPrefix.CONVERSATION, conversation_public_id, "conversation")
    return [_run_out(r) for r in await ledger.list_runs(session, cid, limit=limit)]


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
