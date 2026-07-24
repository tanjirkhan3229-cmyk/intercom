"""Pydantic response models for the ``reporting`` API (P0.9). Read-only; every figure derives from
the ``conversation_metrics`` / ``daily_rollups`` projections, never from ``conversation_parts``.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel


class VolumePoint(BaseModel):
    day: dt.date
    opened: int
    closed: int
    replies: int


class VolumeReport(BaseModel):
    """Conversation volume over time (from ``daily_rollups``)."""

    points: list[VolumePoint]


class FirstResponse(BaseModel):
    median_s: float | None
    p90_s: float | None
    count: int


class ResponsivenessReport(BaseModel):
    """First-response responsiveness percentiles (from ``conversation_metrics``)."""

    first_response: FirstResponse


class CsatReport(BaseModel):
    """CSAT summary over the window: rating count, average, and a 1-5 histogram."""

    count: int
    average: float | None
    distribution: dict[str, int]


class CsatGroup(BaseModel):
    """CSAT for one team or agent. ``key`` is the team/agent public id (``None`` = no team /
    unassigned)."""

    key: str | None
    count: int
    average: float | None


class CsatBreakdownReport(BaseModel):
    """CSAT broken down by team and by agent (P1.7) — from ``conversation_metrics``."""

    by_team: list[CsatGroup]
    by_agent: list[CsatGroup]


class QueueReport(BaseModel):
    """Live queue monitor snapshot (cached ≤10 s; RFC-002 §2 R4/R9)."""

    open: int
    unassigned: int
    longest_wait_s: int | None
    agents_online: int
