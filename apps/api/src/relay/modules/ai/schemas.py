"""Pydantic request/response models for the `ai` API (P1.2).

Two admin surfaces: Neko settings (the per-workspace kill switch + grounding gate + scope) and the
run inspector / replay (the "why did Neko say that?" debugging surface, RFC-003 §8). Public ids are
prefixed base62 strings (``run_``, ``cnv_``); nothing here exposes another tenant's data (RLS).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field

# --- Settings -----------------------------------------------------------------


class AiSettingsOut(BaseModel):
    enabled: bool
    channels: list[str]
    grounding_threshold: float
    max_clarifications: int
    source_kinds: list[str] | None
    persona: str | None
    answer_max_tokens: int


class AiSettingsUpdate(BaseModel):
    enabled: bool | None = None
    channels: list[str] | None = Field(default=None, max_length=8)
    grounding_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    max_clarifications: int | None = Field(default=None, ge=0, le=3)
    source_kinds: list[str] | None = None
    persona: str | None = Field(default=None, max_length=2000)
    answer_max_tokens: int | None = Field(default=None, ge=50, le=2000)


# --- Run inspector / replay (RFC-003 §8) --------------------------------------


class AgentRunOut(BaseModel):
    id: str
    conversation_id: str
    status: str
    outcome: str | None
    handoff_reason: str | None
    language: str | None
    safety_class: str | None
    query: str
    rewritten_query: str | None
    retrieved: list[dict[str, Any]]
    grounding_score: float | None
    prompt_hash: str | None
    provider: str | None
    models: dict[str, Any]
    answer: str | None
    citations: list[str]
    verdict: dict[str, Any]
    tokens: dict[str, Any]
    cost_usd: float
    latency_ms: dict[str, Any]
    created_at: dt.datetime


class ReplayResult(BaseModel):
    """The outcome of re-running a stored turn from its ledger ``trace`` (RFC-003 §8)."""

    run_id: str
    reproducible: bool
    prompt_hash_match: bool
    answer_match: bool
    original_prompt_hash: str | None
    replay_prompt_hash: str | None
    original_answer: str | None
    replay_answer: str | None
