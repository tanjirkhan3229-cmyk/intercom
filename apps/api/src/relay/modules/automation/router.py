"""HTTP routes for the ``automation`` module (P1.5), mounted under ``/v0``.

Workflow + version management, publish, and read-only run + execution-log inspection, plus the
bot-step ``input`` resume. All are admin/agent JWT actions (the service enforces RBAC); there is no
builder UI here (that is P1.6) — these are the API the builder and tests drive.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request, status

from relay.core.deps import CurrentPrincipal, SessionDep
from relay.core.idempotency import idempotent
from relay.core.pagination import Page

from . import schemas, service

router = APIRouter(tags=["automation"])


# --- Workflows ----------------------------------------------------------------


@router.post("/workflows", response_model=schemas.WorkflowOut, status_code=status.HTTP_201_CREATED)
@idempotent(status_code=201)
async def create_workflow(
    req: schemas.WorkflowCreate,
    request: Request,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> schemas.WorkflowOut:
    return await service.create_workflow(session, principal, req)


@router.get("/workflows", response_model=Page[schemas.WorkflowOut])
async def list_workflows(
    _principal: CurrentPrincipal,
    session: SessionDep,
    cursor: str | None = None,
    limit: int | None = Query(default=None, ge=1, le=200),
) -> Page[schemas.WorkflowOut]:
    return await service.list_workflows(session, cursor=cursor, limit=limit)


@router.get("/workflows/{workflow_id}", response_model=schemas.WorkflowOut)
async def get_workflow(
    workflow_id: str, _principal: CurrentPrincipal, session: SessionDep
) -> schemas.WorkflowOut:
    return await service.get_workflow(session, workflow_id)


@router.patch("/workflows/{workflow_id}", response_model=schemas.WorkflowOut)
async def update_workflow(
    workflow_id: str,
    req: schemas.WorkflowUpdate,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> schemas.WorkflowOut:
    return await service.update_workflow(session, principal, workflow_id, req)


# --- Versions -----------------------------------------------------------------


@router.post(
    "/workflows/{workflow_id}/versions",
    response_model=schemas.WorkflowVersionOut,
    status_code=status.HTTP_201_CREATED,
)
@idempotent(status_code=201)
async def create_version(
    workflow_id: str,
    req: schemas.WorkflowVersionCreate,
    request: Request,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> schemas.WorkflowVersionOut:
    return await service.create_version(session, principal, workflow_id, req)


@router.get("/workflows/{workflow_id}/versions", response_model=Page[schemas.WorkflowVersionOut])
async def list_versions(
    workflow_id: str,
    _principal: CurrentPrincipal,
    session: SessionDep,
    cursor: str | None = None,
    limit: int | None = Query(default=None, ge=1, le=200),
) -> Page[schemas.WorkflowVersionOut]:
    return await service.list_versions(session, workflow_id, cursor=cursor, limit=limit)


@router.post("/workflows/{workflow_id}/publish", response_model=schemas.WorkflowOut)
async def publish(
    workflow_id: str,
    req: schemas.PublishRequest,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> schemas.WorkflowOut:
    return await service.publish(session, principal, workflow_id, req)


# --- Runs + execution log -----------------------------------------------------


@router.get("/workflow_runs", response_model=Page[schemas.WorkflowRunOut])
async def list_runs(
    _principal: CurrentPrincipal,
    session: SessionDep,
    workflow_id: str | None = None,
    cursor: str | None = None,
    limit: int | None = Query(default=None, ge=1, le=200),
) -> Page[schemas.WorkflowRunOut]:
    return await service.list_runs(
        session, workflow_public_id=workflow_id, cursor=cursor, limit=limit
    )


@router.get("/workflow_runs/{run_id}", response_model=schemas.WorkflowRunOut)
async def get_run(
    run_id: str, _principal: CurrentPrincipal, session: SessionDep
) -> schemas.WorkflowRunOut:
    return await service.get_run(session, run_id)


@router.get("/workflow_runs/{run_id}/steps", response_model=list[schemas.WorkflowRunStepOut])
async def list_run_steps(
    run_id: str, _principal: CurrentPrincipal, session: SessionDep
) -> list[schemas.WorkflowRunStepOut]:
    return await service.list_run_steps(session, run_id)


@router.post("/workflow_runs/{run_id}/cancel", response_model=schemas.WorkflowRunOut)
async def cancel_run(
    run_id: str, principal: CurrentPrincipal, session: SessionDep
) -> schemas.WorkflowRunOut:
    return await service.cancel_run(session, principal, run_id)


@router.post("/workflow_runs/{run_id}/input", response_model=schemas.WorkflowRunOut)
async def submit_input(
    run_id: str, req: schemas.SubmitInputRequest, principal: CurrentPrincipal
) -> schemas.WorkflowRunOut:
    # No SessionDep: submit_input manages its own transaction so it can enqueue the advance task
    # after the resume commits (commit-then-enqueue).
    return await service.submit_input(principal, run_id, req)
