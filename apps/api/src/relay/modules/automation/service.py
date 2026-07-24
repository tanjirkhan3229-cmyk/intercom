"""Service layer for the ``automation`` module — the workflow engine's control surface (P1.5).

Covers workflow/version CRUD + **publish** (validates the graph, pins it as the active version
without disturbing in-flight runs), run + execution-log reads, run cancellation, ``submit_input``
(resume a parked ``bot_step``), and the two entry points the trigger consumer uses
(``active_workflows_for_trigger`` + ``create_run_from_trigger``).

Everything is RBAC-choke-pointed (``authorize(min_role=Role.ADMIN)`` for mutations) and RLS-scoped.
``submit_input`` manages its own transaction (not the request session) so it can enqueue the resume
task **after commit** — the same commit-then-enqueue discipline the consumer/tasks use.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from relay.core import outbox
from relay.core.db import session_scope
from relay.core.errors import ConflictError, NotFoundError, ValidationError
from relay.core.ids import IdPrefix, decode_public_id, encode_public_id, uuid7
from relay.core.pagination import Page, clamp_limit
from relay.core.principal import Principal
from relay.core.rbac import Role, authorize

from . import events, schemas
from .graph import WorkflowGraph
from .models import Workflow, WorkflowRun, WorkflowRunStep, WorkflowVersion


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _decode_or_404(prefix: str, public_id: str, what: str) -> uuid.UUID:
    try:
        return decode_public_id(prefix, public_id)
    except ValueError as exc:
        raise NotFoundError(f"{what} not found") from exc


# --- DTO builders -------------------------------------------------------------


def workflow_out(w: Workflow) -> schemas.WorkflowOut:
    return schemas.WorkflowOut(
        id=encode_public_id(IdPrefix.WORKFLOW, w.id),
        name=w.name,
        status=w.status,
        active_version_id=(
            encode_public_id(IdPrefix.WORKFLOW_VERSION, w.active_version_id)
            if w.active_version_id
            else None
        ),
        created_at=w.created_at,
        updated_at=w.updated_at,
    )


def version_out(v: WorkflowVersion) -> schemas.WorkflowVersionOut:
    return schemas.WorkflowVersionOut(
        id=encode_public_id(IdPrefix.WORKFLOW_VERSION, v.id),
        workflow_id=encode_public_id(IdPrefix.WORKFLOW, v.workflow_id),
        version=v.version,
        trigger_key=v.trigger_key,
        status=v.status,
        created_at=v.created_at,
    )


def _encode_subject(r: WorkflowRun) -> str | None:
    if r.subject_id is None:
        return None
    if r.subject_kind == "conversation":
        return encode_public_id(IdPrefix.CONVERSATION, r.subject_id)
    if r.subject_kind == "contact":
        return encode_public_id(IdPrefix.CONTACT, r.subject_id)
    return str(r.subject_id)


def run_out(r: WorkflowRun) -> schemas.WorkflowRunOut:
    return schemas.WorkflowRunOut(
        id=encode_public_id(IdPrefix.WORKFLOW_RUN, r.id),
        workflow_id=encode_public_id(IdPrefix.WORKFLOW, r.workflow_id),
        workflow_version_id=encode_public_id(IdPrefix.WORKFLOW_VERSION, r.workflow_version_id),
        status=r.status,
        trigger_topic=r.trigger_topic,
        subject_kind=r.subject_kind,
        subject_id=_encode_subject(r),
        current_node_id=r.current_node_id,
        error=r.error,
        created_at=r.created_at,
        updated_at=r.updated_at,
        completed_at=r.completed_at,
    )


def step_out(s: WorkflowRunStep) -> schemas.WorkflowRunStepOut:
    return schemas.WorkflowRunStepOut(
        id=str(s.id),
        node_id=s.node_id,
        status=s.status,
        action_type=s.action_type,
        result=s.result,
        error=s.error,
        attempt=s.attempt,
        created_at=s.created_at,
        updated_at=s.updated_at,
    )


# --- Workflows: CRUD ----------------------------------------------------------


async def create_workflow(
    session: AsyncSession, principal: Principal, req: schemas.WorkflowCreate
) -> schemas.WorkflowOut:
    authorize(principal, min_role=Role.ADMIN)
    wf = Workflow(workspace_id=principal.workspace_id, name=req.name, status="inactive")
    session.add(wf)
    await session.flush()
    return workflow_out(wf)


async def _get_workflow(session: AsyncSession, workflow_id: uuid.UUID) -> Workflow:
    wf = await session.get(Workflow, workflow_id)
    if wf is None:
        raise NotFoundError("workflow not found")
    return wf


async def get_workflow(session: AsyncSession, public_id: str) -> schemas.WorkflowOut:
    wid = _decode_or_404(IdPrefix.WORKFLOW, public_id, "workflow")
    return workflow_out(await _get_workflow(session, wid))


async def list_workflows(
    session: AsyncSession, *, cursor: str | None = None, limit: int | None = None
) -> Page[schemas.WorkflowOut]:
    n = clamp_limit(limit)
    stmt = select(Workflow)
    if cursor:
        stmt = stmt.where(Workflow.id < _decode_or_404(IdPrefix.WORKFLOW, cursor, "cursor"))
    rows = list((await session.scalars(stmt.order_by(Workflow.id.desc()).limit(n + 1))).all())
    next_cursor = None
    if len(rows) > n:
        rows = rows[:n]
        next_cursor = encode_public_id(IdPrefix.WORKFLOW, rows[-1].id)
    return Page(items=[workflow_out(w) for w in rows], next_cursor=next_cursor)


async def update_workflow(
    session: AsyncSession, principal: Principal, public_id: str, req: schemas.WorkflowUpdate
) -> schemas.WorkflowOut:
    authorize(principal, min_role=Role.ADMIN)
    wf = await _get_workflow(session, _decode_or_404(IdPrefix.WORKFLOW, public_id, "workflow"))
    if req.name is not None:
        wf.name = req.name
    if req.status is not None:
        if req.status == "active" and wf.active_version_id is None:
            raise ConflictError("cannot activate a workflow with no published version")
        wf.status = req.status
    wf.updated_at = _now()
    await session.flush()
    return workflow_out(wf)


# --- Versions -----------------------------------------------------------------


async def create_version(
    session: AsyncSession,
    principal: Principal,
    workflow_public_id: str,
    req: schemas.WorkflowVersionCreate,
) -> schemas.WorkflowVersionOut:
    """Create a draft version from a graph. The graph is validated here (422 with a ``path`` on any
    problem) so a broken graph can never be stored."""
    authorize(principal, min_role=Role.ADMIN)
    wid = _decode_or_404(IdPrefix.WORKFLOW, workflow_public_id, "workflow")
    await _get_workflow(session, wid)
    parsed = WorkflowGraph.from_dict(req.graph)  # raises ValidationError on any problem

    next_version = (
        await session.scalar(
            select(func.coalesce(func.max(WorkflowVersion.version), 0) + 1).where(
                WorkflowVersion.workflow_id == wid
            )
        )
    ) or 1
    version = WorkflowVersion(
        workspace_id=principal.workspace_id,
        workflow_id=wid,
        version=int(next_version),
        graph=req.graph,
        trigger_key=parsed.trigger_key,
        status="draft",
        created_by=principal.admin_id,
    )
    session.add(version)
    try:
        await session.flush()
    except sa.exc.IntegrityError as exc:  # concurrent create raced the version number
        raise ConflictError("version number conflict; retry") from exc
    return version_out(version)


async def list_versions(
    session: AsyncSession,
    workflow_public_id: str,
    *,
    cursor: str | None = None,
    limit: int | None = None,
) -> Page[schemas.WorkflowVersionOut]:
    wid = _decode_or_404(IdPrefix.WORKFLOW, workflow_public_id, "workflow")
    n = clamp_limit(limit)
    stmt = select(WorkflowVersion).where(WorkflowVersion.workflow_id == wid)
    if cursor:
        stmt = stmt.where(
            WorkflowVersion.id < _decode_or_404(IdPrefix.WORKFLOW_VERSION, cursor, "cursor")
        )
    rows = list(
        (await session.scalars(stmt.order_by(WorkflowVersion.id.desc()).limit(n + 1))).all()
    )
    next_cursor = None
    if len(rows) > n:
        rows = rows[:n]
        next_cursor = encode_public_id(IdPrefix.WORKFLOW_VERSION, rows[-1].id)
    return Page(items=[version_out(v) for v in rows], next_cursor=next_cursor)


async def publish(
    session: AsyncSession,
    principal: Principal,
    workflow_public_id: str,
    req: schemas.PublishRequest,
) -> schemas.WorkflowOut:
    """Publish a version: pin it as ``active_version_id`` and activate the workflow. In-flight runs
    keep their pinned version (RFC-001 §6.7) — only *new* triggers use the new active version.

    Only a ``draft``/``published`` version may be (re)published — an ``archived`` one is rejected —
    and the graph is re-validated here as defense-in-depth, so a version can never be activated with
    a graph that fails today's rules (even if it was stored before a rule tightened)."""
    authorize(principal, min_role=Role.ADMIN)
    wid = _decode_or_404(IdPrefix.WORKFLOW, workflow_public_id, "workflow")
    vid = _decode_or_404(IdPrefix.WORKFLOW_VERSION, req.version_id, "version")
    wf = await _get_workflow(session, wid)
    version = await session.get(WorkflowVersion, vid)
    if version is None or version.workflow_id != wid:
        raise NotFoundError("version not found")
    if version.status == "archived":
        raise ConflictError("cannot publish an archived version")
    WorkflowGraph.from_dict(version.graph)  # re-validate (422 with a path) before it goes live
    version.status = "published"
    wf.active_version_id = version.id
    wf.status = "active"
    wf.updated_at = _now()
    await session.flush()
    return workflow_out(wf)


# --- Runs + execution log -----------------------------------------------------


async def _get_run(session: AsyncSession, run_id: uuid.UUID) -> WorkflowRun:
    run = await session.get(WorkflowRun, run_id)
    if run is None:
        raise NotFoundError("workflow run not found")
    return run


async def get_run(session: AsyncSession, public_id: str) -> schemas.WorkflowRunOut:
    rid = _decode_or_404(IdPrefix.WORKFLOW_RUN, public_id, "run")
    return run_out(await _get_run(session, rid))


async def list_runs(
    session: AsyncSession,
    *,
    workflow_public_id: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
) -> Page[schemas.WorkflowRunOut]:
    n = clamp_limit(limit)
    stmt = select(WorkflowRun)
    if workflow_public_id is not None:
        stmt = stmt.where(
            WorkflowRun.workflow_id
            == _decode_or_404(IdPrefix.WORKFLOW, workflow_public_id, "workflow")
        )
    if cursor:
        stmt = stmt.where(WorkflowRun.id < _decode_or_404(IdPrefix.WORKFLOW_RUN, cursor, "cursor"))
    rows = list((await session.scalars(stmt.order_by(WorkflowRun.id.desc()).limit(n + 1))).all())
    next_cursor = None
    if len(rows) > n:
        rows = rows[:n]
        next_cursor = encode_public_id(IdPrefix.WORKFLOW_RUN, rows[-1].id)
    return Page(items=[run_out(r) for r in rows], next_cursor=next_cursor)


async def list_run_steps(
    session: AsyncSession, run_public_id: str
) -> list[schemas.WorkflowRunStepOut]:
    """The execution log: every step of a run in execution order (P1.5 acceptance)."""
    rid = _decode_or_404(IdPrefix.WORKFLOW_RUN, run_public_id, "run")
    await _get_run(session, rid)  # 404 (RLS) if not this workspace's
    steps = (
        await session.scalars(
            select(WorkflowRunStep)
            .where(WorkflowRunStep.run_id == rid)
            .order_by(WorkflowRunStep.created_at, WorkflowRunStep.id)
        )
    ).all()
    return [step_out(s) for s in steps]


async def cancel_run(
    session: AsyncSession, principal: Principal, run_public_id: str
) -> schemas.WorkflowRunOut:
    authorize(principal, min_role=Role.ADMIN)
    rid = _decode_or_404(IdPrefix.WORKFLOW_RUN, run_public_id, "run")
    run = (
        await session.execute(select(WorkflowRun).where(WorkflowRun.id == rid).with_for_update())
    ).scalar_one_or_none()
    if run is None:
        raise NotFoundError("workflow run not found")
    if run.status in ("completed", "failed", "cancelled"):
        raise ConflictError(f"run is already {run.status}")
    run.status = "cancelled"
    run.updated_at = _now()
    await session.flush()
    return run_out(run)


# --- Trigger consumer entry points --------------------------------------------


@dataclass(frozen=True)
class MatchedWorkflow:
    """An active workflow whose pinned version matches a trigger key (for the consumer)."""

    workflow_id: uuid.UUID
    version_id: uuid.UUID
    entry_node_id: str
    trigger_filter: dict[str, Any] | None


async def active_workflows_for_trigger(
    session: AsyncSession, trigger_key: str
) -> list[MatchedWorkflow]:
    """Active workflows in the caller's workspace whose pinned version has this trigger key. RLS
    scopes it to the workspace (the consumer set ``app.ws`` from the event payload)."""
    rows = (
        await session.execute(
            select(Workflow.id, WorkflowVersion.id, WorkflowVersion.graph)
            .join(WorkflowVersion, WorkflowVersion.id == Workflow.active_version_id)
            .where(
                Workflow.status == "active",
                WorkflowVersion.status == "published",  # never fire from an archived active version
                WorkflowVersion.trigger_key == trigger_key,
            )
        )
    ).all()
    matched: list[MatchedWorkflow] = []
    for workflow_id, version_id, graph in rows:
        parsed = WorkflowGraph.load(graph)
        trigger_node = parsed.nodes[parsed.entry]
        matched.append(
            MatchedWorkflow(
                workflow_id=workflow_id,
                version_id=version_id,
                entry_node_id=parsed.entry,
                trigger_filter=trigger_node.get("filter"),
            )
        )
    return matched


async def create_run_from_trigger(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    workflow_id: uuid.UUID,
    version_id: uuid.UUID,
    entry_node_id: str,
    trigger_topic: str,
    dedupe_key: str,
    subject_kind: str | None,
    subject_id: uuid.UUID | None,
    context: dict[str, Any],
) -> uuid.UUID | None:
    """Create a run for a matched trigger, exactly-once. Returns the new run id, or ``None``
    if a run for this ``(workflow, dedupe_key)`` already exists (at-least-once redelivery). Emits
    ``workflow_run.started`` in the same txn (we hold the freshly-inserted run row)."""
    run_id = uuid7()
    stmt = (
        pg_insert(WorkflowRun)
        .values(
            id=run_id,
            workspace_id=workspace_id,
            workflow_id=workflow_id,
            workflow_version_id=version_id,
            status="running",
            trigger_topic=trigger_topic,
            dedupe_key=dedupe_key,
            subject_kind=subject_kind,
            subject_id=subject_id,
            context=context,
            current_node_id=entry_node_id,
        )
        .on_conflict_do_nothing(
            index_elements=[
                WorkflowRun.workspace_id,
                WorkflowRun.workflow_id,
                WorkflowRun.dedupe_key,
            ]
        )
        .returning(WorkflowRun.id)
    )
    created = (await session.execute(stmt)).scalar_one_or_none()
    if created is None:
        return None
    await outbox.emit(
        session,
        aggregate=events.AGGREGATE_WORKFLOW_RUN,
        aggregate_id=run_id,
        topic=events.WORKFLOW_RUN_STARTED,
        payload={
            "workspace_id": encode_public_id(IdPrefix.WORKSPACE, workspace_id),
            "workflow_id": encode_public_id(IdPrefix.WORKFLOW, workflow_id),
            "run_id": encode_public_id(IdPrefix.WORKFLOW_RUN, run_id),
            "status": "running",
        },
    )
    return run_id


# --- Bot-step resume ----------------------------------------------------------


def _subject_conversation_id(run: WorkflowRun) -> uuid.UUID:
    pid = run.context.get("conversation_id")
    if not isinstance(pid, str):
        raise ConflictError("run has no conversation subject")
    return decode_public_id(IdPrefix.CONVERSATION, pid)


def _subject_contact_id(run: WorkflowRun) -> uuid.UUID:
    pid = run.context.get("contact_id")
    if not isinstance(pid, str):
        raise ConflictError("run has no contact subject")
    return decode_public_id(IdPrefix.CONTACT, pid)


async def submit_input(
    principal: Principal, run_public_id: str, req: schemas.SubmitInputRequest
) -> schemas.WorkflowRunOut:
    """Resume a run parked on a ``bot_step`` with the contact's answer, then enqueue its advance.

    Manages its own transaction (not the request session) so the advance is enqueued *after* the
    resume commits — otherwise the worker could read the run before the reply landed.
    """
    from relay.modules.crm import service as crm_service
    from relay.modules.messaging import service as messaging_service

    authorize(principal, min_role=Role.AGENT)
    rid = _decode_or_404(IdPrefix.WORKFLOW_RUN, run_public_id, "run")

    async with session_scope(principal.workspace_id) as session:
        run = (
            await session.execute(
                select(WorkflowRun).where(WorkflowRun.id == rid).with_for_update()
            )
        ).scalar_one_or_none()
        if run is None:
            raise NotFoundError("workflow run not found")
        if run.status != "awaiting_input":
            raise ConflictError(f"run is not awaiting input (status={run.status})")
        if run.current_node_id != req.node_id:
            raise ConflictError("run is awaiting input on a different node")

        version = await session.get(WorkflowVersion, run.workflow_version_id)
        if version is None:  # pragma: no cover
            raise NotFoundError("workflow version not found")
        node = WorkflowGraph.load(version.graph).get(req.node_id)
        if node is None or node.get("type") != "bot_step":  # pragma: no cover - guarded above
            raise ConflictError("node is not a bot step")

        params = node.get("params") or {}
        bot = node["bot"]
        if bot in ("ask_buttons", "disambiguate"):
            match = next((o for o in params["options"] if o["value"] == req.value), None)
            if match is not None:
                next_id = match["next"]
            elif isinstance(params.get("default_next"), str):
                next_id = params["default_next"]
            else:
                raise ValidationError(f"unknown option value {req.value!r}")
            # Reassign (not in-place mutate) so SQLAlchemy persists the plain-JSONB ``context``.
            answers = {**run.context.get("answers", {}), req.node_id: req.value}
            run.context = {**run.context, "answers": answers}
        else:  # collect — store the reply into the configured attribute
            if params["target"] == "conversation":
                await messaging_service.system_set_conversation_attribute(
                    session,
                    conversation_id=_subject_conversation_id(run),
                    key=params["key"],
                    value=req.value,
                )
            else:
                await crm_service.system_set_contact_attribute(
                    session, contact_id=_subject_contact_id(run), key=params["key"], value=req.value
                )
            collected = {**run.context.get("collected", {}), params["key"]: req.value}
            run.context = {**run.context, "collected": collected}
            next_id = params["next"]

        await session.execute(
            update(WorkflowRunStep)
            .where(WorkflowRunStep.run_id == run.id, WorkflowRunStep.node_id == req.node_id)
            .values(status="done", result={"value": req.value}, updated_at=_now())
        )
        run.current_node_id = next_id
        run.status = "running"
        run.updated_at = _now()
        result = run_out(run)
        workspace_id = run.workspace_id

    _enqueue_advance(workspace_id, rid)
    return result


def _enqueue_advance(workspace_id: uuid.UUID, run_id: uuid.UUID) -> None:
    from relay.worker import celery_app

    celery_app.send_task(
        "automation.advance_run", args=[str(workspace_id), str(run_id)], queue="interactive"
    )
