"""Pydantic request/response schemas for the ``automation`` public API (P1.5).

Public ids (``wfl_``/``wfv_``/``wfr_`` + subject ids) are encoded/decoded in the service layer;
schema fields are plain strings. Graph validation is the service's job (via
``graph.validate_graph``)
so the 422 carries a ``path`` into the offending node.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field


class WorkflowCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class WorkflowUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    status: str | None = Field(default=None, pattern="^(inactive|active)$")


class WorkflowOut(BaseModel):
    id: str
    name: str
    status: str
    active_version_id: str | None
    created_at: dt.datetime
    updated_at: dt.datetime


class WorkflowVersionCreate(BaseModel):
    graph: dict[str, Any]


class WorkflowVersionOut(BaseModel):
    id: str
    workflow_id: str
    version: int
    trigger_key: str
    status: str
    created_at: dt.datetime


class PublishRequest(BaseModel):
    version_id: str


class WorkflowRunOut(BaseModel):
    id: str
    workflow_id: str
    workflow_version_id: str
    status: str
    trigger_topic: str
    subject_kind: str | None
    subject_id: str | None
    current_node_id: str | None
    error: str | None
    created_at: dt.datetime
    updated_at: dt.datetime
    completed_at: dt.datetime | None


class WorkflowRunStepOut(BaseModel):
    id: str
    node_id: str
    status: str
    action_type: str | None
    result: dict[str, Any]
    error: str | None
    attempt: int
    created_at: dt.datetime
    updated_at: dt.datetime


class SubmitInputRequest(BaseModel):
    """A contact's answer to a parked ``bot_step`` — the option ``value`` (ask_buttons/disambiguate)
    or the free-text reply (collect)."""

    node_id: str = Field(min_length=1)
    value: str = Field(min_length=1, max_length=4000)
