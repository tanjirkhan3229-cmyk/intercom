"""Workflow run executor — the step engine (P1.5, RFC-001 §6.7).

``advance(session, run)`` walks a run's pinned graph node-by-node, in the caller's transaction, with
the run row already **FOR UPDATE-locked** (so same-run advances serialise — there is never a
concurrent advance of one run, and cross-run advances never contend). It runs until it hits a
*suspending* node (external ``call_webhook`` action, a ``bot_step`` awaiting input, or a
``wait``) or a terminal ``end``.

**Exactly-once effects.** Every node writes a ``workflow_run_steps`` row keyed UNIQUE ``(run_id,
node_id)``. An internal effect is performed **iff** its step row is freshly claimed, in the *same
transaction* as the claim — so a replayed advance (Celery redelivery, crash-recovery, reaper)
sees the committed row and skips the effect. That, plus the "each node runs at most once per run"
invariant, is what gives the P1.5 chaos guarantee: kill mid-run / duplicate trigger / broker flush ⇒
zero duplicate side effects.

The executor never makes network calls itself: an external ``call_webhook`` is *suspended* and
handed to the ``automation.run_action`` task (``tasks.py``) on the outbound-HTTP bulkhead. It
returns the node ids that need that enqueue (done by the calling task *after commit*).
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from relay.core import outbox
from relay.core.errors import ConflictError, NotFoundError, ValidationError
from relay.core.ids import IdPrefix, decode_public_id, encode_public_id, uuid7
from relay.core.predicates import evaluate
from relay.modules.crm import service as crm_service
from relay.modules.messaging import service as messaging_service
from relay.settings import get_settings

from . import events
from .graph import WorkflowGraph
from .models import Timer, WorkflowRun, WorkflowRunStep, WorkflowVersion

# Business-logic "can't apply this effect" errors → the step is recorded ``skipped`` and the run
# continues; anything else (infra) propagates so the task retries (acks_late) and the reaper
# backs up.
_SKIPPABLE = (NotFoundError, ConflictError, ValidationError)

_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# --- context id resolution ----------------------------------------------------


def _conversation_uuid(run: WorkflowRun) -> uuid.UUID:
    pid = run.context.get("conversation_id")
    if not isinstance(pid, str):
        raise NotFoundError(
            "workflow action needs a conversation subject, but the trigger had none"
        )
    try:
        return decode_public_id(IdPrefix.CONVERSATION, pid)
    except ValueError as exc:  # malformed id → skippable (not an infra crash-loop)
        raise ValidationError(f"malformed conversation id {pid!r}") from exc


def _contact_uuid(run: WorkflowRun) -> uuid.UUID:
    pid = run.context.get("contact_id")
    if not isinstance(pid, str):
        raise NotFoundError("workflow action needs a contact subject, but the trigger had none")
    try:
        return decode_public_id(IdPrefix.CONTACT, pid)
    except ValueError as exc:
        raise ValidationError(f"malformed contact id {pid!r}") from exc


def _opt_uuid(prefix: str, pid: Any) -> uuid.UUID | None:
    if not isinstance(pid, str) or not pid:
        return None
    try:
        return decode_public_id(prefix, pid)
    except ValueError as exc:
        raise ValidationError(f"invalid id {pid!r}") from exc


def _render(template: str, context: dict[str, Any]) -> str:
    """Interpolate ``{{ dotted.path }}`` placeholders from the run context.

    Missing paths and non-scalar resolutions (dict/list/None) render to empty string, so a template
    that accidentally references a whole object (e.g. a webhook-result dict) never leaks a Python
    ``repr`` into a customer-facing message body — only scalar leaves are substituted."""

    def sub(m: re.Match[str]) -> str:
        cur: Any = context
        for part in m.group(1).split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return ""
        return str(cur) if isinstance(cur, (str, int, float)) else ""  # bool is an int subclass

    return _VAR_RE.sub(sub, template)


def _fire_at(params: dict[str, Any], now: dt.datetime) -> dt.datetime:
    """Resolve a wait/snooze duration to an absolute instant (validated at publish time)."""
    seconds = params.get("seconds")
    if isinstance(seconds, int) and not isinstance(seconds, bool):
        return now + dt.timedelta(seconds=seconds)
    until = params.get("until")
    if isinstance(until, str):
        parsed = dt.datetime.fromisoformat(until.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    raise ValidationError("wait/snooze node missing 'seconds' or 'until'")


# --- ledger -------------------------------------------------------------------


async def _claim_step(
    session: AsyncSession, run: WorkflowRun, node_id: str, action_type: str
) -> bool:
    """Insert the ledger row for (run, node) as ``started``. Returns True iff we won the insert
    (the effect should run); False if it already exists (replay → skip)."""
    stmt = (
        pg_insert(WorkflowRunStep)
        .values(
            id=uuid7(),
            workspace_id=run.workspace_id,
            run_id=run.id,
            node_id=node_id,
            status="started",
            action_type=action_type,
        )
        .on_conflict_do_nothing(index_elements=[WorkflowRunStep.run_id, WorkflowRunStep.node_id])
        .returning(WorkflowRunStep.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def _mark_step(
    session: AsyncSession,
    run: WorkflowRun,
    node_id: str,
    status: str,
    *,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    await session.execute(
        update(WorkflowRunStep)
        .where(WorkflowRunStep.run_id == run.id, WorkflowRunStep.node_id == node_id)
        .values(status=status, result=result or {}, error=error, updated_at=_now())
    )


async def _get_step(
    session: AsyncSession, run: WorkflowRun, node_id: str
) -> WorkflowRunStep | None:
    return (
        await session.execute(
            select(WorkflowRunStep).where(
                WorkflowRunStep.run_id == run.id, WorkflowRunStep.node_id == node_id
            )
        )
    ).scalar_one_or_none()


async def _emit_run_event(session: AsyncSession, run: WorkflowRun, topic: str) -> None:
    """Emit a run-lifecycle event on the outbox (we hold the run's row lock → seq is safe)."""
    await outbox.emit(
        session,
        aggregate=events.AGGREGATE_WORKFLOW_RUN,
        aggregate_id=run.id,
        topic=topic,
        payload={
            "workspace_id": encode_public_id(IdPrefix.WORKSPACE, run.workspace_id),
            "workflow_id": encode_public_id(IdPrefix.WORKFLOW, run.workflow_id),
            "run_id": encode_public_id(IdPrefix.WORKFLOW_RUN, run.id),
            "status": run.status,
        },
    )


# --- graph loading ------------------------------------------------------------


async def _load_graph(session: AsyncSession, run: WorkflowRun) -> WorkflowGraph:
    version = await session.get(WorkflowVersion, run.workflow_version_id)
    if version is None:  # pragma: no cover - versions are immutable and never deleted
        raise NotFoundError("workflow version not found")
    return WorkflowGraph.load(version.graph)


# --- the step loop ------------------------------------------------------------


async def advance(session: AsyncSession, run: WorkflowRun) -> list[str]:
    """Advance ``run`` (already FOR UPDATE-locked, status ``running``) as far as it can go this txn.

    Returns the node ids of freshly-suspended ``call_webhook`` actions that the caller must enqueue
    on the action task *after commit*. Mutates ``run`` (status / current_node_id / context) and the
    ledger in ``session``; the caller owns the commit.
    """
    settings = get_settings()
    graph = await _load_graph(session, run)
    enqueue_actions: list[str] = []
    budget = settings.workflow_run_step_budget

    for _ in range(budget):
        node_id = run.current_node_id
        node = graph.get(node_id) if node_id else None
        if node is None:
            return await _fail(session, run, f"unknown node {node_id!r}", enqueue_actions)
        ntype = node["type"]

        if ntype == "trigger":
            run.current_node_id = node["next"]
            continue

        if ntype == "end":
            run.status = "completed"
            run.completed_at = _now()
            run.updated_at = _now()
            await _emit_run_event(session, run, events.WORKFLOW_RUN_COMPLETED)
            return enqueue_actions

        if ntype == "condition":
            await _run_condition(session, run, node)
            continue

        if ntype == "wait":
            if await _enter_wait(session, run, node):
                return enqueue_actions  # parked on a durable timer
            run.current_node_id = node["next"]  # already waited (defensive; graphs are acyclic)
            continue

        if ntype == "bot_step":
            await _enter_bot(session, run, node)
            return enqueue_actions

        if ntype == "action":
            directive = await _run_action_node(session, run, node)
            if directive == "continue":
                run.current_node_id = node["next"]
                continue
            if directive in ("suspend", "inflight"):
                # Both hand the external POST to run_action. "inflight" (re-entry while a prior
                # attempt's step is still 'started') also re-enqueues: if the original run_action
                # message was lost, this recovers it immediately; the per-attempt lease dedupes a
                # genuinely-in-flight attempt, so it can never double-fire the POST.
                enqueue_actions.append(node["id"])
                run.updated_at = _now()
                return enqueue_actions
            # "fail"
            return await _fail(session, run, f"action node {node['id']} failed", enqueue_actions)

        return await _fail(session, run, f"unhandled node type {ntype!r}", enqueue_actions)

    return await _fail(session, run, "step budget exceeded (possible cycle)", enqueue_actions)


async def _fail(
    session: AsyncSession, run: WorkflowRun, error: str, enqueue_actions: list[str]
) -> list[str]:
    run.status = "failed"
    run.error = error
    run.updated_at = _now()
    await _emit_run_event(session, run, events.WORKFLOW_RUN_FAILED)
    return enqueue_actions


async def _run_condition(session: AsyncSession, run: WorkflowRun, node: dict[str, Any]) -> None:
    node_id = node["id"]
    if await _claim_step(session, run, node_id, "condition"):
        branch = "true" if evaluate(node["predicate"], run.context) else "false"
        await _mark_step(session, run, node_id, "done", result={"branch": branch})
    else:
        step = await _get_step(session, run, node_id)
        branch = (step.result or {}).get("branch", "false") if step else "false"
    run.current_node_id = node["true"] if branch == "true" else node["false"]
    run.updated_at = _now()


async def _enter_wait(session: AsyncSession, run: WorkflowRun, node: dict[str, Any]) -> bool:
    """Place a durable timer and park the run; return ``True`` when parked. Returns ``False`` if the
    wait's ledger row already exists (it already fired) — the caller then advances past it rather
    than re-parking with no pending timer (which would strand the run). Graphs are validated
    acyclic, so this replay branch is defensive: after a fresh wait commits, the run is ``waiting``
    and a redelivered advance is skipped by the status guard — the only way back here is a
    back-edge, which validation forbids."""
    node_id = node["id"]
    if not await _claim_step(session, run, node_id, "wait"):
        return False
    fire_at = _fire_at(node.get("params") or {}, _now())
    session.add(
        Timer(
            id=uuid7(),
            workspace_id=run.workspace_id,
            run_id=run.id,
            node_id=node_id,
            fire_at=fire_at,
            status="pending",
        )
    )
    await _mark_step(session, run, node_id, "done", result={"fire_at": fire_at.isoformat()})
    run.current_node_id = node["next"]
    run.status = "waiting"
    run.updated_at = _now()
    return True


async def _enter_bot(session: AsyncSession, run: WorkflowRun, node: dict[str, Any]) -> None:
    """Post the bot prompt to the run's conversation and park awaiting the contact's reply. Resumed
    by ``service.submit_input``. If the run has no conversation subject the step is skipped and the
    run fails (a bot step on a contact-only trigger is a design error)."""
    node_id = node["id"]
    if not await _claim_step(session, run, node_id, f"bot:{node['bot']}"):
        run.status = "awaiting_input"
        run.updated_at = _now()
        return
    params = node.get("params") or {}
    try:
        conv_id = _conversation_uuid(run)
    except NotFoundError as exc:
        await _mark_step(session, run, node_id, "failed", error=str(exc))
        run.status = "failed"
        run.error = str(exc)
        run.updated_at = _now()
        await _emit_run_event(session, run, events.WORKFLOW_RUN_FAILED)
        return
    meta = {
        "workflow": {
            "run_id": encode_public_id(IdPrefix.WORKFLOW_RUN, run.id),
            "node_id": node_id,
            "bot": node["bot"],
            "options": [
                {"label": o["label"], "value": o["value"]} for o in params.get("options", [])
            ],
        }
    }
    await messaging_service.system_post_message(
        session, conversation_id=conv_id, body=_render(params["prompt"], run.context), meta=meta
    )
    run.status = "awaiting_input"
    run.current_node_id = node_id
    run.updated_at = _now()


async def _run_action_node(session: AsyncSession, run: WorkflowRun, node: dict[str, Any]) -> str:
    """Execute an action node. Returns a directive: ``continue`` (advance to next), ``suspend``
    (freshly-claimed external call — caller enqueues run_action), ``inflight`` (external call
    already in flight), or ``fail``."""
    settings = get_settings()
    node_id = node["id"]
    action = node["action"]

    if action == "apply_sla" and not settings.workflow_sla_action_enabled:
        # Registered but flag-gated off until P1.7 (RFC-000 §5). Record + move on.
        if await _claim_step(session, run, node_id, action):
            await _mark_step(
                session, run, node_id, "skipped", error="apply_sla disabled (lands in P1.7)"
            )
        return "continue"

    if action == "call_webhook":
        return await _handle_call_webhook(session, run, node)

    # Internal effect: claim, then perform in the same txn (atomic → exactly-once on replay).
    if await _claim_step(session, run, node_id, action):
        try:
            result = await _perform_internal(session, run, node)
            await _mark_step(session, run, node_id, "done", result=result)
        except _SKIPPABLE as exc:
            await _mark_step(session, run, node_id, "skipped", error=str(exc))
    return "continue"


async def _handle_call_webhook(
    session: AsyncSession, run: WorkflowRun, node: dict[str, Any]
) -> str:
    node_id = node["id"]
    step = await _get_step(session, run, node_id)
    if step is None:
        # Fresh: claim + suspend; the caller enqueues automation.run_action after commit.
        await _claim_step(session, run, node_id, "call_webhook")
        run.status = "suspended"
        return "suspend"
    if step.status == "done":
        # Reassign (not in-place mutate): ``context`` is a plain JSONB column, so SQLAlchemy only
        # persists a change if the attribute is reassigned. Expose the response to later conditions.
        run.context = {**run.context, node_id: step.result}
        return "continue"
    if step.status in ("failed", "skipped"):
        return "fail" if step.status == "failed" else "continue"
    # "started": the action task is (or will be) running it — stay suspended.
    run.status = "suspended"
    return "inflight"


async def _perform_internal(
    session: AsyncSession, run: WorkflowRun, node: dict[str, Any]
) -> dict[str, Any]:
    """Perform a non-external action via the sanctioned system entry points. Returns a small result
    dict recorded on the ledger step."""
    action = node["action"]
    params = node.get("params") or {}

    if action == "assign":
        await messaging_service.system_assign(
            session,
            conversation_id=_conversation_uuid(run),
            assignee_id=_opt_uuid(IdPrefix.ADMIN, params.get("assignee_id")),
            team_id=_opt_uuid(IdPrefix.TEAM, params.get("team_id")),
        )
    elif action == "route_to_team":
        await messaging_service.system_route_to_team(
            session,
            conversation_id=_conversation_uuid(run),
            team_id=decode_public_id(IdPrefix.TEAM, params["team_id"]),
        )
    elif action == "add_tag":
        await messaging_service.system_add_tag(
            session, conversation_id=_conversation_uuid(run), name=params["name"]
        )
    elif action == "set_attribute":
        if params["target"] == "conversation":
            await messaging_service.system_set_conversation_attribute(
                session,
                conversation_id=_conversation_uuid(run),
                key=params["key"],
                value=params["value"],
            )
        else:
            await crm_service.system_set_contact_attribute(
                session, contact_id=_contact_uuid(run), key=params["key"], value=params["value"]
            )
    elif action == "snooze":
        await messaging_service.system_change_state(
            session,
            conversation_id=_conversation_uuid(run),
            target="snoozed",
            snoozed_until=_fire_at(params, _now()),
        )
    elif action == "close":
        await messaging_service.system_change_state(
            session, conversation_id=_conversation_uuid(run), target="closed"
        )
    elif action == "send_reply":
        part_id = await messaging_service.system_post_message(
            session,
            conversation_id=_conversation_uuid(run),
            body=_render(params["body"], run.context),
        )
        return {"part_id": encode_public_id(IdPrefix.PART, part_id)}
    elif action == "hand_to_aide":
        await messaging_service.system_set_ai_status(
            session, conversation_id=_conversation_uuid(run), status=params.get("status", "active")
        )
    return {}
