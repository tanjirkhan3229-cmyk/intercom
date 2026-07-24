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


class QueueReport(BaseModel):
    """Live queue monitor snapshot (cached ≤10 s; RFC-002 §2 R4/R9)."""

    open: int
    unassigned: int
    longest_wait_s: int | None
    agents_online: int


# --- Neko analytics (P1.4, RFC-003 §8) — all figures from ``neko_daily_rollups`` -----------------


class NekoDailyPoint(BaseModel):
    """One day of Neko activity (a row of ``neko_daily_rollups``). ``avg_latency_ms`` is None on a
    day with no timed runs; ``resolutions`` is the billing meter's net (can dip negative on a day
    that only carried a claw-back)."""

    day: dt.date
    runs_total: int
    runs_answered: int
    runs_clarify: int
    runs_handoff: int
    runs_ineligible: int
    runs_error: int
    conversations_engaged: int
    conversations_answered: int
    conversations_handoff: int
    resolutions: float
    cost_usd: float
    avg_latency_ms: float | None


class NekoTotals(BaseModel):
    """Window totals + derived rates. Rates are None when there is nothing to divide by."""

    runs_total: int
    conversations_engaged: int
    conversations_answered: int
    conversations_handoff: int
    resolutions: float
    # resolutions / engaged conversations (billing-grade resolution rate, RFC-003 §8).
    resolution_rate: float | None
    # share of engaged conversations Neko handled without a human = (engaged - handoff)/engaged.
    deflection_rate: float | None
    cost_usd: float
    avg_cost_per_conversation: float | None
    avg_latency_ms: float | None
    # Merged handoff-reason histogram over the window ({reason: count}).
    handoff_reasons: dict[str, int]


class NekoReport(BaseModel):
    """Neko analytics over the window: per-day series (resolution/deflection/cost/latency over time)
    plus window totals incl. the handoff-reasons breakdown."""

    points: list[NekoDailyPoint]
    totals: NekoTotals


class CsatBucket(BaseModel):
    """CSAT for one cohort: rating count, average, and a 1-5 histogram."""

    count: int
    average: float | None
    distribution: dict[str, int]


class NekoCsatReport(BaseModel):
    """CSAT delta (RFC-003 §8): Neko-touched conversations vs the rest, over ratings in the window.
    ``delta`` = neko.average - non_neko.average (None unless both cohorts have ratings)."""

    neko_touched: CsatBucket
    non_neko: CsatBucket
    delta: float | None
