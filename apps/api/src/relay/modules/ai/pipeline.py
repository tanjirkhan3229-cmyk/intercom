"""The Neko turn pipeline — the RFC-003 §3 state machine, on the ai.interactive queue.

    preflight → query-rewrite → retrieve → grounding gate → generate (streamed) → verify → emit

Every edge has a timeout and a fallback (RFC-003 §5); the turn never dead-ends a customer (RFC-003
§1): low grounding ⇒ one clarifying question then handoff; a safety flag or an explicit "talk to a
person" ⇒ instant handoff; a verifier rejection ⇒ the drafted answer is dropped and a human takes
over; provider exhaustion ⇒ handoff, never silence. Every terminal writes one ``agent_runs`` row
(RFC-003 §3) with a fully replayable ``trace``.

Injection posture (RFC-003 §6): retrieved chunks + customer text are framed as delimited DATA and
never as instructions (see :mod:`prompts`); generation must cite chunk ids and the verifier rejects
ungrounded claims; retrieval runs under the same RLS/``app.ws`` regime as everything else, so the
model can never be prompted into another tenant's corpus.
"""

from __future__ import annotations

import datetime as dt
import json
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from relay.core.db import session_scope
from relay.core.ids import IdPrefix, encode_public_id
from relay.core.logging import get_logger
from relay.modules.ai import ledger, metering, prompts, protocol, streaming
from relay.modules.ai.prompts import EvidenceChunk, label_for
from relay.modules.ai.providers import TokenUsage, distinctive_terms
from relay.modules.ai.resilience import AllProvidersFailed, LLMRouter, StreamOutcome, get_router
from relay.modules.identity import service as identity_service
from relay.modules.knowledge import service as knowledge_service
from relay.modules.messaging import service as messaging_service
from relay.settings import Settings, get_settings

log = get_logger(__name__)

_NEGATIVE_TERMS = frozenset(
    {
        "angry",
        "frustrated",
        "annoyed",
        "terrible",
        "awful",
        "useless",
        "worst",
        "unacceptable",
        "ridiculous",
        "disappointed",
        "hate",
        "broken",
        "still",
        "again",
        "never",
    }
)


@dataclass(frozen=True)
class TurnResult:
    """The turn's terminal state (returned to the task/tests; not the ledger row itself)."""

    outcome: str
    run_id: uuid.UUID | None = None
    part_id: uuid.UUID | None = None
    answer: str | None = None
    handoff_reason: str | None = None
    reason: str | None = None  # why an ``ineligible`` turn was skipped (no row written)


def _now() -> float:
    return time.monotonic()


def _ms(since: float) -> float:
    return round((time.monotonic() - since) * 1000.0, 2)


def _parse_json(text: str) -> dict[str, Any]:
    """Lenient parse of a model's JSON response (tolerates prose around the object)."""
    try:
        return json.loads(text)  # type: ignore[no-any-return]
    except (json.JSONDecodeError, TypeError):
        pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start : end + 1])  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            return {}
    return {}


def _term_grounded(term: str, evidence_terms: set[str]) -> bool:
    """A query term is grounded if it appears in the evidence — exactly, or as a morphological
    variant (substring either way, e.g. ``refund``↔``refunds``, ``cancel``↔``cancellation``). The
    coarse-but-robust match keeps the gate method-agnostic; the verifier does the fine check."""
    if term in evidence_terms:
        return True
    if len(term) < 4:
        return False
    return any(len(e) >= 4 and (term in e or e in term) for e in evidence_terms)


def grounding_score(query: str, chunks: Iterable[EvidenceChunk]) -> float:
    """Coarse pre-answer grounding signal in ``[0, 1]``: the fraction of distinctive query terms
    grounded in the retrieved evidence (RFC-003 §5 gate). Method-agnostic, so it survives a re-tuned
    retriever; the fine-grained groundedness check is the post-generation verifier."""
    q = distinctive_terms(query)
    if not q:
        return 0.0
    evidence_terms: set[str] = set()
    for c in chunks:
        evidence_terms |= distinctive_terms(c.content)
    grounded = sum(1 for term in q if _term_grounded(term, evidence_terms))
    return grounded / len(q)


def _summary(recent: list[messaging_service.AiTurnPart], *, max_chars: int = 600) -> str | None:
    """A rolling conversation summary for prompt context (RFC-003 §9 context trimming) — the recent
    comments, oldest→newest, role-tagged, truncated. Cheap + deterministic; a model summary can
    replace it later."""
    lines: list[str] = []
    for p in recent:
        if p.part_type == "comment" and p.body:
            who = "Customer" if p.author_kind == "contact" else "Neko"
            lines.append(f"{who}: {p.body.strip()}")
    if not lines:
        return None
    text = "\n".join(lines)
    return text[-max_chars:] if len(text) > max_chars else text


def _sentiment(recent: list[messaging_service.AiTurnPart]) -> str:
    for p in recent:
        if p.author_kind == "contact" and p.body and distinctive_terms(p.body) & _NEGATIVE_TERMS:
            return "frustrated"
    return "neutral"


def _last_customer_text(recent: list[messaging_service.AiTurnPart]) -> str | None:
    for p in reversed(recent):
        if p.author_kind == "contact" and p.part_type == "comment" and p.body:
            return p.body.strip()
    return None


async def _noop_publish(_channel: str, _data: dict[str, Any]) -> None:
    """Sandbox stream sink: a preview turn streams nowhere (no gateway, no Redis)."""
    return None


def _matches_handoff_intent(text: str, intents: list[str]) -> str | None:
    """Return the first always-handoff intent (RFC-003 §5) the customer text matches (case-
    insensitive substring — a tenant lists phrases like "cancel my account"), or None."""
    lowered = text.lower()
    for intent in intents:
        needle = intent.strip().lower()
        if needle and needle in lowered:
            return intent
    return None


@dataclass
class _Turn:
    """Mutable state threaded through one turn's stages; the terminal helpers commit + finalize."""

    workspace_id: uuid.UUID
    conversation_id: uuid.UUID
    trigger_part_id: uuid.UUID
    run_id: uuid.UUID
    conv_public: str
    workspace_name: str
    customer_text: str
    settings_view: ledger.AiSettingsView
    stream: streaming.TurnStream
    router: LLMRouter
    settings: Settings
    started: float
    prior_clarifications: int
    recent: list[messaging_service.AiTurnPart]

    # Spend cap (RFC-003 §9): set in Phase A when the workspace is over its monthly cap — the turn
    # routes to a human before any model call (checked at the top of ``_run_stages``).
    over_spend_cap: bool = False
    # Preview sandbox (P1.3): when set, the terminals persist NOTHING (no part, ledger, meter,
    # stream) — they stash the would-be ledger record in ``sandbox_record`` and return.
    sandbox: bool = False
    sandbox_record: ledger.LedgerRecord | None = None

    # accumulated
    history_summary: str | None = None
    language: str | None = None
    safety_class: str | None = None
    rewritten_query: str | None = None
    evidence: list[EvidenceChunk] = field(default_factory=list)
    retrieved_meta: list[dict[str, Any]] = field(default_factory=list)
    grounding: float | None = None
    prompt_hash: str | None = None
    provider: str | None = None
    models: dict[str, Any] = field(default_factory=dict)
    tokens: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    latency: dict[str, Any] = field(default_factory=dict)
    raw_answer: str | None = None
    verdict: dict[str, Any] = field(default_factory=dict)

    # -- accounting helpers --
    def _account(
        self,
        stage: str,
        *,
        provider: str,
        model: str,
        usage: TokenUsage,
        cost: float,
        latency_ms: float,
    ) -> None:
        self.models[stage] = {"provider": provider, "model": model}
        self.tokens[stage] = {"in": usage.input_tokens, "out": usage.output_tokens}
        self.latency[stage] = latency_ms
        self.cost_usd += cost

    def _trace(self) -> dict[str, Any]:
        return {
            "customer_text": self.customer_text,
            "workspace_name": self.workspace_name,
            "persona": self.settings_view.persona,
            "history_summary": self.history_summary,
            "answer_max_tokens": self.settings_view.answer_max_tokens,
            "rewritten_query": self.rewritten_query,
            "evidence": [
                {
                    "label": e.label,
                    "chunk_id": str(e.chunk_id),
                    "source_id": str(e.source_id),
                    "source_kind": e.source_kind,
                    "title": e.title,
                    "heading_path": e.heading_path,
                    "content": e.content,
                    "score": e.score,
                }
                for e in self.evidence
            ],
            "raw_answer": self.raw_answer,
        }

    def _base_record(self, outcome: str, **kw: Any) -> ledger.LedgerRecord:
        self.latency["total"] = _ms(self.started)
        return ledger.LedgerRecord(
            outcome=outcome,
            language=self.language,
            safety_class=self.safety_class,
            rewritten_query=self.rewritten_query,
            retrieved=self.retrieved_meta,
            grounding_score=self.grounding,
            prompt_hash=self.prompt_hash,
            provider=self.provider,
            models=self.models,
            tokens=self.tokens,
            cost_usd=round(self.cost_usd, 8),
            latency_ms=self.latency,
            verdict=self.verdict,
            trace=self._trace(),
            **kw,
        )

    # -- terminals --
    async def _commit_answer(self, answer: str, citations: list[str]) -> TurnResult:
        if self.sandbox:
            self.sandbox_record = self._base_record("answered", answer=answer, citations=citations)
            return TurnResult(outcome="answered", run_id=self.run_id, answer=answer)
        async with session_scope(self.workspace_id) as session:
            part = await messaging_service.append_ai_reply(
                session,
                conversation_id=self.conversation_id,
                body=answer,
                meta={
                    "run_id": encode_public_id(IdPrefix.AGENT_RUN, self.run_id),
                    "citations": citations,
                    "author": "neko",
                },
            )
            await messaging_service.set_ai_status(
                session, conversation_id=self.conversation_id, status="active"
            )
            await ledger.finalize(
                session,
                self.run_id,
                self._base_record("answered", answer=answer, citations=citations, part_id=part.id),
            )
        await self.stream.end(encode_public_id(IdPrefix.PART, part.id))
        return TurnResult(outcome="answered", run_id=self.run_id, part_id=part.id, answer=answer)

    async def _commit_clarify(self) -> TurnResult:
        question = prompts.clarifying_question(self.customer_text)
        if self.sandbox:
            self.sandbox_record = self._base_record("clarify", answer=question)
            return TurnResult(outcome="clarify", run_id=self.run_id, answer=question)
        async with session_scope(self.workspace_id) as session:
            part = await messaging_service.append_ai_reply(
                session,
                conversation_id=self.conversation_id,
                body=question,
                meta={
                    "run_id": encode_public_id(IdPrefix.AGENT_RUN, self.run_id),
                    "author": "neko",
                    "clarifying": True,
                },
            )
            await messaging_service.set_ai_status(
                session, conversation_id=self.conversation_id, status="active"
            )
            await ledger.finalize(
                session, self.run_id, self._base_record("clarify", answer=question, part_id=part.id)
            )
        await self.stream.end(encode_public_id(IdPrefix.PART, part.id))
        return TurnResult(outcome="clarify", run_id=self.run_id, part_id=part.id, answer=question)

    async def _commit_handoff(self, reason: str, *, outcome: str = "handoff") -> TurnResult:
        if self.sandbox:
            self.sandbox_record = self._base_record(outcome, handoff_reason=reason)
            return TurnResult(outcome=outcome, run_id=self.run_id, handoff_reason=reason)
        await self.stream.superseded(reason)
        note = prompts.handoff_summary_note(
            recap=_summary(self.recent) or self.customer_text,
            sources_tried=[f"{e.source_kind}: {e.title or e.label}" for e in self.evidence],
            sentiment=_sentiment(self.recent),
            reason=reason,
        )
        async with session_scope(self.workspace_id) as session:
            # Customer-facing line first ("never a dead end"), then the private recap for the human.
            await messaging_service.append_ai_reply(
                session,
                conversation_id=self.conversation_id,
                body=prompts.handoff_message(),
                meta={"run_id": encode_public_id(IdPrefix.AGENT_RUN, self.run_id), "handoff": True},
            )
            note_part = await messaging_service.append_ai_note(
                session,
                conversation_id=self.conversation_id,
                body=note,
                meta={"run_id": encode_public_id(IdPrefix.AGENT_RUN, self.run_id)},
            )
            await messaging_service.set_ai_status(
                session, conversation_id=self.conversation_id, status="handed_off"
            )
            await ledger.finalize(
                session,
                self.run_id,
                self._base_record(outcome, handoff_reason=reason, part_id=note_part.id),
            )
        return TurnResult(
            outcome=outcome, run_id=self.run_id, part_id=note_part.id, handoff_reason=reason
        )

    async def _commit_ineligible(self, reason: str) -> TurnResult:
        if self.sandbox:
            self.sandbox_record = self._base_record("ineligible", handoff_reason=reason)
            return TurnResult(outcome="ineligible", run_id=self.run_id, reason=reason)
        async with session_scope(self.workspace_id) as session:
            await ledger.finalize(
                session, self.run_id, self._base_record("ineligible", handoff_reason=reason)
            )
        return TurnResult(outcome="ineligible", run_id=self.run_id, reason=reason)


async def run_turn(
    *,
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    trigger_part_id: uuid.UUID,
    router: LLMRouter | None = None,
    settings: Settings | None = None,
    stream_publish: streaming.Publish | None = None,
) -> TurnResult:
    """Run one Neko turn end-to-end (RFC-003 §3). Idempotent per ``trigger_part_id``."""
    settings = settings or get_settings()
    router = router or get_router()
    started = _now()

    # --- Phase A: eligibility + claim + context (short txn) -----------------------------------
    async with session_scope(workspace_id) as session:
        ctx = await messaging_service.ai_turn_context(session, conversation_id)
        if ctx is None:
            return TurnResult(outcome="ineligible", reason="conversation_not_found")
        settings_view = await ledger.load_settings(session, workspace_id, settings=settings)

        # Kill switches (RFC-003 §6): global first, then per-workspace, then channel scope + status.
        if not settings.ai_global_enabled or settings.ai_model_route == "off":
            return TurnResult(outcome="ineligible", reason="global_off")
        if not settings_view.enabled:
            return TurnResult(outcome="ineligible", reason="workspace_disabled")
        if ctx.channel not in settings_view.channels:
            return TurnResult(outcome="ineligible", reason="channel_out_of_scope")
        if ctx.ai_status == "handed_off":
            return TurnResult(outcome="ineligible", reason="already_handed_off")

        customer_text = _last_customer_text(ctx.recent)
        if not customer_text:
            return TurnResult(outcome="ineligible", reason="no_customer_text")

        run_id = await ledger.claim(
            session,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            trigger_part_id=trigger_part_id,
            query=customer_text,
        )
        if run_id is None:  # a concurrent/redelivered turn already owns it (exactly-once gate)
            return TurnResult(outcome="ineligible", reason="already_processed")

        ws_ref = await identity_service.get_workspace_ref(session, workspace_id)
        workspace_name = ws_ref.name if ws_ref else "Support"
        prior_clarifications = await ledger.count_clarifications(session, conversation_id)
        recent = list(ctx.recent)

        # Spend cap (RFC-003 §9): if the workspace is past its monthly cap, Neko routes this turn to
        # a human (below, in _run_stages) instead of answering — never a silent drop — and admins
        # are notified once/month. Checked live per turn so a breach flips routing within one turn.
        over_spend_cap = await metering.is_over_spend_cap(
            session, workspace_id, settings_view.monthly_spend_cap_usd
        )
        if over_spend_cap:
            await metering.notify_spend_cap_reached(session, workspace_id)

    conv_public = encode_public_id(IdPrefix.CONVERSATION, conversation_id)
    turn = _Turn(
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        trigger_part_id=trigger_part_id,
        run_id=run_id,
        conv_public=conv_public,
        workspace_name=workspace_name,
        customer_text=customer_text,
        settings_view=settings_view,
        stream=streaming.TurnStream(conv_public, publish=stream_publish),
        router=router,
        settings=settings,
        started=started,
        prior_clarifications=prior_clarifications,
        recent=recent,
        over_spend_cap=over_spend_cap,
    )
    return await _run_stages(turn)


async def _run_stages(turn: _Turn) -> TurnResult:
    settings = turn.settings
    history = _summary(turn.recent)
    turn.history_summary = history

    # --- Spend cap (RFC-003 §9): route to a human before any model call -----------------------
    if turn.over_spend_cap:
        return await turn._commit_handoff("spend_cap_reached")

    # --- Always-handoff intents (RFC-003 §5): route without spending a model call --------------
    matched_intent = _matches_handoff_intent(
        turn.customer_text, turn.settings_view.always_handoff_intents
    )
    if matched_intent:
        return await turn._commit_handoff("always_handoff_intent")

    # --- Preflight (cheap, ≤400 ms) -----------------------------------------------------------
    t = _now()
    try:
        pf = await turn.router.complete(
            tier="cheap",
            messages=prompts.preflight_messages(turn.customer_text),
            max_tokens=64,
            timeout_s=settings.ai_preflight_timeout_seconds,
        )
    except AllProvidersFailed:
        return await turn._commit_handoff("provider_unavailable", outcome="error")
    turn._account(
        "preflight",
        provider=pf.provider,
        model=pf.model,
        usage=pf.response.usage,
        cost=pf.cost_usd,
        latency_ms=_ms(t),
    )
    pf_data = _parse_json(pf.response.text)
    turn.language = str(pf_data.get(protocol.PREFLIGHT_LANGUAGE, "en"))
    turn.safety_class = str(pf_data.get(protocol.PREFLIGHT_SAFETY, "ok"))

    if bool(pf_data.get(protocol.PREFLIGHT_HANDOFF)):
        return await turn._commit_handoff("explicit_request")  # "talk to a person" — instant
    if turn.safety_class in ("abuse", "self_harm"):
        return await turn._commit_handoff(f"safety_{turn.safety_class}")
    if not bool(pf_data.get(protocol.PREFLIGHT_IS_QUESTION, True)):
        return await turn._commit_ineligible("not_a_question")  # Neko stays quiet

    # --- Query rewrite (cheap) ----------------------------------------------------------------
    t = _now()
    try:
        rw = await turn.router.complete(
            tier="cheap",
            messages=prompts.rewrite_messages(turn.customer_text, history),
            max_tokens=64,
            timeout_s=settings.ai_rewrite_timeout_seconds,
        )
    except AllProvidersFailed:
        return await turn._commit_handoff("provider_unavailable", outcome="error")
    turn._account(
        "rewrite",
        provider=rw.provider,
        model=rw.model,
        usage=rw.response.usage,
        cost=rw.cost_usd,
        latency_ms=_ms(t),
    )
    turn.rewritten_query = str(
        _parse_json(rw.response.text).get(protocol.REWRITE_QUERY) or turn.customer_text
    )

    # --- Retrieve (P1.1, RLS-scoped) ----------------------------------------------------------
    t = _now()
    async with session_scope(turn.workspace_id) as session:
        chunks = await knowledge_service.retrieve_chunks(
            session,
            workspace_id=turn.workspace_id,
            query=turn.rewritten_query,
            k=settings.ai_retrieval_k,
            source_kinds=turn.settings_view.source_kinds,
        )
    turn.latency["retrieve"] = _ms(t)
    turn.evidence = [
        EvidenceChunk(
            label=label_for(i),
            chunk_id=c.chunk_id,
            source_id=c.source_id,
            source_kind=c.source_kind,
            content=c.content,
            title=c.title,
            heading_path=c.heading_path,
            score=c.score,
        )
        for i, c in enumerate(chunks)
    ]
    turn.retrieved_meta = [
        {
            "label": e.label,
            "chunk_id": str(e.chunk_id),
            "source_id": str(e.source_id),
            "source_kind": e.source_kind,
            "score": e.score,
        }
        for e in turn.evidence
    ]

    # --- Grounding gate (RFC-003 §5) ----------------------------------------------------------
    turn.grounding = grounding_score(turn.rewritten_query, turn.evidence)
    if turn.grounding < turn.settings_view.grounding_threshold:
        if turn.prior_clarifications < turn.settings_view.max_clarifications:
            return await turn._commit_clarify()
        return await turn._commit_handoff("insufficient_grounding")

    # --- Generate (frontier, streamed) --------------------------------------------------------
    gen_msgs = prompts.generation_messages(
        workspace_name=turn.workspace_name,
        customer_text=turn.customer_text,
        chunks=turn.evidence,
        persona=turn.settings_view.persona,
        history_summary=history,
    )
    turn.prompt_hash = prompts.prompt_hash(gen_msgs)
    outcome = StreamOutcome()
    await turn.stream.start(encode_public_id(IdPrefix.AGENT_RUN, turn.run_id))
    buf: list[str] = []
    t = _now()
    try:
        async for chunk in turn.router.stream(
            tier="frontier",
            messages=gen_msgs,
            outcome=outcome,
            max_tokens=turn.settings_view.answer_max_tokens,
            timeout_s=settings.ai_generate_timeout_seconds,
            first_token_timeout=settings.ai_first_token_timeout_seconds,
        ):
            if chunk.delta:
                buf.append(chunk.delta)
                await turn.stream.delta(chunk.delta)
    except AllProvidersFailed:
        return await turn._commit_handoff("provider_unavailable", outcome="error")
    turn.latency["generate"] = _ms(t)
    turn.latency["first_token"] = outcome.first_token_ms
    turn.provider = outcome.provider
    turn.models["generate"] = {"provider": outcome.provider, "model": outcome.model}
    turn.tokens["generate"] = {"in": outcome.usage.input_tokens, "out": outcome.usage.output_tokens}
    turn.cost_usd += outcome.cost_usd
    turn.raw_answer = "".join(buf)

    citation_labels = protocol.parse_citations(turn.raw_answer)
    label_to_chunk = {e.label: str(e.chunk_id) for e in turn.evidence}
    cited = [label_to_chunk[label] for label in citation_labels if label in label_to_chunk]
    answer_text = protocol.strip_citations(turn.raw_answer)

    # Generation MUST cite its evidence (RFC-003 §6). An uncited answer is treated as ungrounded.
    if not cited:
        return await turn._commit_handoff("no_citation")

    # --- Verify (cheap) — groundedness + policy filters ---------------------------------------
    t = _now()
    try:
        vr = await turn.router.complete(
            tier="cheap",
            messages=prompts.verify_messages(turn.raw_answer, turn.evidence),
            max_tokens=192,
            timeout_s=settings.ai_verify_timeout_seconds,
        )
    except AllProvidersFailed:
        return await turn._commit_handoff("provider_unavailable", outcome="error")
    turn._account(
        "verify",
        provider=vr.provider,
        model=vr.model,
        usage=vr.response.usage,
        cost=vr.cost_usd,
        latency_ms=_ms(t),
    )
    turn.verdict = _parse_json(vr.response.text)
    if not bool(turn.verdict.get(protocol.VERIFY_GROUNDED)):
        # The planted-ungrounded-claim rejection (RFC-003 acceptance): drop the draft, hand off.
        return await turn._commit_handoff("verify_reject")

    return await turn._commit_answer(answer_text, cited)


async def sandbox_run(
    *,
    workspace_id: uuid.UUID,
    message: str,
    history: list[tuple[str, str]] | None = None,
    router: LLMRouter | None = None,
    settings: Settings | None = None,
) -> ledger.LedgerRecord:
    """Run a turn against the workspace's *current* knowledge WITHOUT persisting anything — the
    preview sandbox (P1.3, RFC-003 §5). Same stages, same decisions, same retrieval trace as a real
    turn, but no part / ledger row / meter / stream: admins see *why* an answer happened. The
    returned ``LedgerRecord`` carries the identical ``retrieved`` + ``trace`` an ``agent_runs`` row
    would (so the sandbox trace matches ``agent_runs`` — the acceptance check).

    ``history`` is an optional list of ``(role, body)`` where role is ``"customer"``/``"neko"``.
    The spend cap is deliberately NOT enforced here (previewing must never be blocked)."""
    settings = settings or get_settings()
    router = router or get_router()
    started = _now()
    async with session_scope(workspace_id) as session:
        settings_view = await ledger.load_settings(session, workspace_id, settings=settings)
        ws_ref = await identity_service.get_workspace_ref(session, workspace_id)
        workspace_name = ws_ref.name if ws_ref else "Support"

    now = dt.datetime.now(dt.UTC)
    recent = [
        messaging_service.AiTurnPart(
            author_kind="contact" if role == "customer" else "ai_agent",
            part_type="comment",
            body=body,
            created_at=now,
        )
        for role, body in (history or [])
    ]
    recent.append(
        messaging_service.AiTurnPart(
            author_kind="contact", part_type="comment", body=message, created_at=now
        )
    )
    turn = _Turn(
        workspace_id=workspace_id,
        conversation_id=uuid.uuid4(),
        trigger_part_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        conv_public="sandbox",
        workspace_name=workspace_name,
        customer_text=message,
        settings_view=settings_view,
        stream=streaming.TurnStream("sandbox", publish=_noop_publish),
        router=router,
        settings=settings,
        started=started,
        prior_clarifications=0,
        recent=recent,
        sandbox=True,
    )
    await _run_stages(turn)
    assert turn.sandbox_record is not None  # every terminal stashes one in sandbox mode
    return turn.sandbox_record
