"""Celery tasks for the ``automation`` module (P1.5, RFC-001 §6.4/§6.7).

- ``automation.advance_run``   (queue ``interactive``) — drive the executor for one run under its
  row lock. Fast internal steps only; an external ``call_webhook`` is offloaded to ``run_action``.
- ``automation.run_action``    (queue ``webhooks``) — the external ``call_webhook`` POST via the
  SSRF-guarded client, with a per-host circuit breaker + bounded jittered retry. Kept off the
  ``interactive`` queue so a slow/hung endpoint never starves run advancement.
- ``automation.fire_timer``    (queue ``interactive``) — mark a due timer fired + resume its run.
- ``automation.scan_due_timers`` (beat, ``housekeeping``) — claim due timers across workspaces via
  ``relay_claim_due_timers`` (FOR UPDATE SKIP LOCKED) and enqueue ``fire_timer``. Durable in
  Postgres, so waits survive a broker flush.
- ``automation.scan_stuck_runs`` (beat, ``housekeeping``) — the **reaper**: re-drive running/
  suspended runs whose in-flight message was lost (broker flush) so every run completes or parks
  resumably.

Pure-DB tasks run async via ``run_coro`` (matching channels/tasks); ``run_action`` is sync where it
does the blocking HTTP (matching webhooks/tasks). Every task is idempotent — the
``workflow_run_steps`` ledger + the run's status guard make redelivery a no-op.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select, text, update

from relay.core.asyncio_bridge import run_coro
from relay.core.breaker import RedisCircuitBreaker
from relay.core.db import session_scope
from relay.core.ids import IdPrefix, encode_public_id
from relay.core.logging import get_logger
from relay.core.redis import get_redis_sync
from relay.core.ssrf import SsrfError, guarded_post
from relay.settings import get_settings
from relay.worker import celery_app

from . import executor
from .graph import WorkflowGraph
from .models import Timer, WorkflowRun, WorkflowRunStep, WorkflowVersion

log = get_logger(__name__)

_SCAN_BATCH = 200


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# --- advance_run --------------------------------------------------------------


@celery_app.task(name="automation.advance_run", queue="interactive", acks_late=True)
def advance_run(workspace_id: str, run_id: str) -> str:
    return run_coro(_advance_run(uuid.UUID(workspace_id), uuid.UUID(run_id)))


async def _advance_run(ws: uuid.UUID, run_id: uuid.UUID) -> str:
    if not get_settings().workflows_enabled:
        return "disabled"
    enqueue: list[str] = []
    async with session_scope(ws) as session:
        run = (
            await session.execute(
                select(WorkflowRun).where(WorkflowRun.id == run_id).with_for_update()
            )
        ).scalar_one_or_none()
        if run is None:
            return "gone"
        if run.status != "running":
            # Parked (waiting/awaiting_input/suspended) or terminal — a redelivery or a race. No-op.
            return f"skip:{run.status}"
        enqueue = await executor.advance(session, run)
    for node_id in enqueue:  # freshly-suspended call_webhook actions → run on the outbound bulkhead
        celery_app.send_task(
            "automation.run_action", args=[str(ws), str(run_id), node_id], queue="webhooks"
        )
    return "advanced"


# --- fire_timer ---------------------------------------------------------------


@celery_app.task(name="automation.fire_timer", queue="interactive", acks_late=True)
def fire_timer(workspace_id: str, timer_id: str, run_id: str) -> str:
    return run_coro(_fire_timer(uuid.UUID(workspace_id), uuid.UUID(timer_id), uuid.UUID(run_id)))


async def _fire_timer(ws: uuid.UUID, timer_id: uuid.UUID, run_id: uuid.UUID) -> str:
    resume = False
    async with session_scope(ws) as session:
        timer = (
            await session.execute(select(Timer).where(Timer.id == timer_id).with_for_update())
        ).scalar_one_or_none()
        if timer is None or timer.status != "pending":
            return "skip"  # already fired/cancelled (idempotent)
        timer.status = "fired"
        run = (
            await session.execute(
                select(WorkflowRun).where(WorkflowRun.id == run_id).with_for_update()
            )
        ).scalar_one_or_none()
        if run is not None and run.status == "waiting":
            run.status = "running"
            run.updated_at = _now()
            resume = True
    if resume:
        celery_app.send_task(
            "automation.advance_run", args=[str(ws), str(run_id)], queue="interactive"
        )
    return "resumed" if resume else "fired"


# --- run_action (external call_webhook) ---------------------------------------


@celery_app.task(name="automation.run_action", queue="webhooks", acks_late=True)
def run_action(workspace_id: str, run_id: str, node_id: str) -> str:
    return run_coro(_run_action(uuid.UUID(workspace_id), uuid.UUID(run_id), node_id))


async def _run_action(ws: uuid.UUID, rid: uuid.UUID, node_id: str) -> str:
    """Execute a suspended ``call_webhook`` action. Idempotent + never leaves a run stuck: on
    success the run resumes at ``next``; on permanent failure (or past the retry cap) the step is
    marked failed and the run is resumed so the executor fails it (centralised failure handling); a
    transient failure leaves the run ``suspended`` and the **reaper** (``scan_stuck_runs``) re-runs
    it once the per-attempt lease lapses — retries are reaper-driven, never self-enqueued, so no two
    ``run_action`` invocations race the same node (which would double-fire the POST). The attempt
    cap counts only *real* HTTP attempts (a breaker-open re-drive is not counted). Retry count lives
    on the step (``attempt``), so the task needs no Celery ``bind``. Blocking HTTP + breaker run in
    a worker thread so the loop stays free."""
    if not get_settings().workflows_enabled:
        return "disabled"
    ctx = await _load_action_ctx(ws, rid, node_id)
    if ctx is None:
        # Stale (run advanced / step already finished) — expected under at-least-once redelivery.
        return "not_actionable"
    made_request, ok, permanent, result = await asyncio.to_thread(_do_http, ctx)
    return await _after_action(
        ws,
        rid,
        node_id,
        ctx["attempt"],
        made_request=made_request,
        ok=ok,
        permanent=permanent,
        result=result,
    )


def _do_http(ctx: dict[str, Any]) -> tuple[bool, bool, bool, dict[str, Any]]:
    """The blocking part of a ``call_webhook``: circuit breaker + SSRF-guarded POST. Returns
    ``(made_request, ok, permanent, result)`` — ``made_request`` is False when the breaker was open
    (no POST issued), so the caller doesn't count it against the retry cap. Sync (runs in a
    thread) — uses the sync Redis breaker + httpx."""
    settings = get_settings()
    breaker = RedisCircuitBreaker(
        get_redis_sync(),
        f"wf-action:{ctx['host']}",
        threshold=settings.workflow_breaker_threshold,
        cooldown_seconds=settings.workflow_breaker_cooldown_seconds,
    )
    if breaker.is_open():
        # No POST issued — the reaper re-drives after the breaker cooldown; this does NOT burn a
        # real attempt (so a per-host breaker tripped by other runs can't fail this one early).
        return (False, False, False, {"error": "circuit breaker open"})
    try:
        resp = guarded_post(
            ctx["url"],
            content=ctx["body"],
            headers=ctx["headers"],
            timeout=settings.workflow_action_timeout_seconds,
            allow_private=settings.webhook_allow_private_targets,
        )
        code = resp.status_code
        if 200 <= code < 300:
            breaker.record_success()
            return (True, True, False, {"status_code": code})
        # 4xx (except 408 Request Timeout / 429 Too Many Requests) is a permanent client error —
        # our request is bad, retrying won't help, and it must NOT trip the per-host breaker (that
        # reflects host health, shared across all workflows). Fail fast without recording a failure.
        if 400 <= code < 500 and code not in (408, 429):
            return (True, False, True, {"error": f"HTTP {code}"})
        breaker.record_failure()  # 5xx / 408 / 429 → transient, host-health signal
        return (True, False, False, {"error": f"HTTP {code}"})
    except SsrfError as exc:  # a bad/blocked URL is permanent — don't waste retries
        breaker.record_failure()
        return (True, False, True, {"error": f"ssrf: {exc.message}"})
    except httpx.HTTPError as exc:
        breaker.record_failure()
        return (True, False, False, {"error": f"transport: {type(exc).__name__}"})
    except Exception as exc:  # never-raise contract: any surprise is a delivery failure
        breaker.record_failure()
        log.error("automation.run_action.unexpected", host=ctx.get("host"), error=str(exc))
        return (True, False, False, {"error": f"error: {type(exc).__name__}"})


async def _after_action(
    ws: uuid.UUID,
    rid: uuid.UUID,
    node_id: str,
    attempt: int,
    *,
    made_request: bool,
    ok: bool,
    permanent: bool,
    result: dict[str, Any],
) -> str:
    """Record the action outcome and either resume the run or leave it for a reaper retry.

    Retries are driven **solely by the reaper** (``scan_stuck_runs`` re-enqueues ``run_action`` for
    suspended runs once the per-attempt lease has lapsed), never by a self-enqueue — so there is
    never a second in-flight ``run_action`` racing the first (which would double-fire the external
    POST). A breaker-open cycle (``made_request`` False) issued no POST, so its claim's attempt bump
    is rolled back and it does not count toward the cap. On a transient failure the step is left
    ``started`` + run ``suspended``; the reaper picks it up after the stale window. On permanent
    failure (or past the attempt cap) the step is failed and the run resumed so the executor fails
    it centrally.
    """
    settings = get_settings()
    if ok:
        await _finish_action(ws, rid, node_id, result, success=True)
        _enqueue_advance(ws, rid)
        return "done"
    if not made_request:
        # Breaker was open — no real attempt happened; un-count the claim's attempt bump so a
        # per-host breaker can't exhaust this run's retry budget. Reaper re-drives after cooldown.
        await _uncount_attempt(ws, rid, node_id)
        return "breaker_open"
    if permanent or attempt >= settings.workflow_action_max_retries:
        await _finish_action(ws, rid, node_id, result, success=False)
        _enqueue_advance(ws, rid)
        return "failed"
    return "retry"  # left suspended; the reaper re-drives after the lease lapses


async def _uncount_attempt(ws: uuid.UUID, rid: uuid.UUID, node_id: str) -> None:
    """Roll back the attempt bump a breaker-open claim made (it issued no POST). Leaves the lease
    (``updated_at``) intact so a concurrent re-drive still backs off until it lapses."""
    async with session_scope(ws) as session:
        await session.execute(
            update(WorkflowRunStep)
            .where(
                WorkflowRunStep.run_id == rid,
                WorkflowRunStep.node_id == node_id,
                WorkflowRunStep.status == "started",
                WorkflowRunStep.attempt > 0,
            )
            .values(attempt=WorkflowRunStep.attempt - 1)
        )


def _enqueue_advance(ws: uuid.UUID, rid: uuid.UUID) -> None:
    celery_app.send_task("automation.advance_run", args=[str(ws), str(rid)], queue="interactive")


def _safe_send_task(name: str, args: list[str], queue: str) -> None:
    """Enqueue a task, swallowing a transient broker error so one failed row in a beat-scan loop
    can't strand the rest of the batch. Anything missed is re-driven by the next scan (timers by
    the lease, runs by the reaper)."""
    try:
        celery_app.send_task(name, args=args, queue=queue)
    except Exception as exc:  # broker blip — log + continue; the scan is a durable backstop
        log.warning("automation.enqueue_failed", task=name, args=args, error=str(exc))


async def _load_action_ctx(ws: uuid.UUID, rid: uuid.UUID, node_id: str) -> dict[str, Any] | None:
    """Claim + read everything needed for the outbound POST (no txn held during the call). Returns
    ``None`` if the run/step is not in the actionable ``suspended``/``started`` state, or if another
    worker holds a **live lease** on this attempt.

    The claim is exclusive: it runs under the run's ``FOR UPDATE`` lock (concurrent ``run_action``
    invocations serialise), and it refuses a node whose step was claimed less than
    ``workflow_action_lease_seconds`` ago (a POST is in flight) — this is what prevents a reaper
    re-drive from double-firing the external call. Claiming bumps ``step.attempt`` (real HTTP
    attempts) and both ``step.updated_at`` (the lease) and ``run.updated_at`` (resets the reaper's
    stale window so it won't re-enqueue while we work).
    """
    async with session_scope(ws) as session:
        run = (
            await session.execute(
                select(WorkflowRun).where(WorkflowRun.id == rid).with_for_update()
            )
        ).scalar_one_or_none()
        if run is None or run.status != "suspended" or run.current_node_id != node_id:
            return None
        step = (
            await session.execute(
                select(WorkflowRunStep).where(
                    WorkflowRunStep.run_id == rid, WorkflowRunStep.node_id == node_id
                )
            )
        ).scalar_one_or_none()
        if step is None or step.status != "started":
            return None
        lease = dt.timedelta(seconds=get_settings().workflow_action_lease_seconds)
        if step.attempt > 0 and step.updated_at > _now() - lease:
            return None  # a prior attempt holds a live lease — its POST may be in flight
        version = await session.get(WorkflowVersion, run.workflow_version_id)
        if version is None:  # pragma: no cover
            return None
        node = WorkflowGraph.load(version.graph).get(node_id)
        if node is None:  # pragma: no cover
            return None
        params = node.get("params") or {}
        url = params["url"]
        headers = {
            **(params.get("headers") or {}),
            "Content-Type": "application/json",
            "User-Agent": "Relay-Workflows/1.0",
        }
        body = json.dumps(
            {
                "topic": run.trigger_topic,
                "run_id": encode_public_id(IdPrefix.WORKFLOW_RUN, rid),
                "workflow_id": encode_public_id(IdPrefix.WORKFLOW, run.workflow_id),
                "context": run.context,
                "data": params.get("body"),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        attempt = step.attempt + 1
        now = _now()
        await session.execute(
            update(WorkflowRunStep)
            .where(WorkflowRunStep.run_id == rid, WorkflowRunStep.node_id == node_id)
            .values(attempt=attempt, updated_at=now)  # the lease
        )
        run.updated_at = now  # reset the reaper's stale window while this attempt runs
        return {
            "url": url,
            "headers": headers,
            "body": body,
            "host": urlsplit(url).hostname or "",
            "attempt": attempt,
        }


async def _finish_action(
    ws: uuid.UUID, rid: uuid.UUID, node_id: str, result: dict[str, Any], *, success: bool
) -> None:
    """Record the action outcome + resume the run. Idempotent: a duplicate (the step already left
    ``started``) is a no-op. On success the run moves to the node's ``next``; on failure the run is
    resumed with the step ``failed`` so the executor fails it centrally."""
    async with session_scope(ws) as session:
        run = (
            await session.execute(
                select(WorkflowRun).where(WorkflowRun.id == rid).with_for_update()
            )
        ).scalar_one_or_none()
        if run is None:
            return
        step = (
            await session.execute(
                select(WorkflowRunStep).where(
                    WorkflowRunStep.run_id == rid, WorkflowRunStep.node_id == node_id
                )
            )
        ).scalar_one_or_none()
        if step is None or step.status != "started":
            return  # already finished (idempotent)
        version = await session.get(WorkflowVersion, run.workflow_version_id)
        node = WorkflowGraph.load(version.graph).get(node_id) if version else None
        if success:
            step.status = "done"
            step.result = result
            step.updated_at = _now()
            run.current_node_id = node["next"] if node else run.current_node_id
            run.status = "running"
            # Expose the response to later condition nodes (reassign — plain JSONB isn't tracked
            # in-place). The executor advances straight to node["next"], so this is the only place
            # the webhook result reaches the run context.
            run.context = {**run.context, node_id: result}
        else:
            step.status = "failed"
            step.result = result
            step.error = result.get("error")
            step.updated_at = _now()
            run.status = "running"  # executor will re-hit the node, see 'failed', and fail the run
        run.updated_at = _now()


# --- beat scans ---------------------------------------------------------------


@celery_app.task(name="automation.scan_due_timers", queue="housekeeping")
def scan_due_timers() -> int:
    return run_coro(_scan_due_timers())


async def _scan_due_timers() -> int:
    """Claim due timers across all workspaces (FOR UPDATE SKIP LOCKED, via the SECURITY DEFINER
    function) and enqueue ``fire_timer`` for each."""
    settings = get_settings()
    if not settings.workflows_enabled:
        return 0
    async with session_scope(None) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT workspace_id, id, run_id, node_id FROM relay_claim_due_timers(:m, :l)"
                ),
                {"m": _SCAN_BATCH, "l": settings.workflow_timer_lease_seconds},
            )
        ).all()
    for workspace_id, timer_id, run_id, _node_id in rows:
        # Per-row guard: a broker blip on one enqueue must not strand the rest of the claimed batch
        # (the lease reclaims any that slip through on the next scan).
        _safe_send_task(
            "automation.fire_timer",
            [str(workspace_id), str(timer_id), str(run_id)],
            "interactive",
        )
    if rows:
        log.info("automation.timers.claimed", count=len(rows))
    return len(rows)


@celery_app.task(name="automation.scan_stuck_runs", queue="housekeeping")
def scan_stuck_runs() -> int:
    return run_coro(_scan_stuck_runs())


async def _scan_stuck_runs() -> int:
    """Reaper: re-drive runs whose in-flight message was lost (e.g. a broker flush). ``running`` →
    re-advance; ``suspended`` → re-run its external action. ``waiting``/``awaiting_input`` runs are
    parked legitimately and excluded by the SQL."""
    settings = get_settings()
    if not settings.workflows_enabled:
        return 0
    async with session_scope(None) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT workspace_id, id, status, current_node_id "
                    "FROM relay_due_workflow_runs(:m, :s)"
                ),
                {"m": _SCAN_BATCH, "s": settings.workflow_run_stale_seconds},
            )
        ).all()
    for workspace_id, run_id, status, node_id in rows:
        if status == "running":
            _safe_send_task(
                "automation.advance_run", [str(workspace_id), str(run_id)], "interactive"
            )
        elif status == "suspended" and node_id:
            _safe_send_task(
                "automation.run_action", [str(workspace_id), str(run_id), node_id], "webhooks"
            )
    if rows:
        log.info("automation.runs.reaped", count=len(rows))
    return len(rows)
