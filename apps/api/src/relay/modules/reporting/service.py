"""Service layer for the ``reporting`` module (P0.9) — the cross-module interface.

Read-only reporting over the projections maintained by the ``reporting-metrics`` consumer and the
daily rollup task. **No query here touches ``conversation_parts``** (P0.9 acceptance): volume + CSAT
read ``daily_rollups`` (composable across days), responsiveness reads ``conversation_metrics``
(percentiles don't compose across day-rows), and the queue monitor reads the conversation *head*
via ``messaging.service.queue_snapshot`` plus Redis presence — all off the hot firehose.

RBAC is enforced here through the single ``authorize`` choke point (RFC-001 §10).
"""

from __future__ import annotations

import datetime as dt
import uuid

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from relay.core import realtime
from relay.core.errors import NotFoundError
from relay.core.ids import IdPrefix, decode_public_id, encode_public_id
from relay.core.principal import Principal
from relay.core.rbac import Role, authorize
from relay.core.redis import get_redis
from relay.modules.messaging import service as messaging_service

from . import schemas
from .models import ConversationMetric, DailyRollup, NekoDailyRollup

# Queue monitor cache: served from Redis with a short TTL so the endpoint is O(1) and the head
# counts refresh at most every ``QUEUE_CACHE_TTL_SECONDS`` (RFC-002 §2 R4: cached counts).
QUEUE_CACHE_PREFIX = "reporting:queue:"
QUEUE_CACHE_TTL_SECONDS = 10


def _team_uuid(team_id: str | None) -> uuid.UUID | None:
    if team_id is None:
        return None
    try:
        return decode_public_id(IdPrefix.TEAM, team_id)
    except ValueError as exc:
        raise NotFoundError("team not found") from exc


def _range(date_from: dt.date | None, date_to: dt.date | None) -> tuple[dt.date, dt.date]:
    """Default to the trailing 30 days (UTC) when unbounded."""
    to = date_to or dt.datetime.now(dt.UTC).date()
    frm = date_from or (to - dt.timedelta(days=29))
    return frm, to


def _opened_day() -> sa.ColumnElement[dt.date]:
    """``opened_at`` bucketed to a UTC calendar day — matches the rollup's day boundary."""
    return sa.cast(sa.func.timezone("UTC", ConversationMetric.opened_at), sa.Date)


def _rated_day() -> sa.ColumnElement[dt.date]:
    """``rated_at`` bucketed to a UTC calendar day — a rating is counted on the day it happened."""
    return sa.cast(sa.func.timezone("UTC", ConversationMetric.rated_at), sa.Date)


def _rate(numerator: float, denominator: int) -> float | None:
    """A ratio rounded to 4 dp, or None when there's nothing to divide by (avoids div-by-zero)."""
    return round(numerator / denominator, 4) if denominator else None


async def volume(
    session: AsyncSession,
    principal: Principal,
    *,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    team_id: str | None = None,
) -> schemas.VolumeReport:
    """Opened/closed/replies per day, summed across teams (or one team) — from ``daily_rollups``."""
    authorize(principal, min_role=Role.AGENT)
    frm, to = _range(date_from, date_to)
    team = _team_uuid(team_id)
    stmt = (
        select(
            DailyRollup.day,
            func.sum(DailyRollup.conversations_opened),
            func.sum(DailyRollup.conversations_closed),
            func.sum(DailyRollup.replies_count),
        )
        .where(DailyRollup.day >= frm, DailyRollup.day <= to)
        .group_by(DailyRollup.day)
        .order_by(DailyRollup.day)
    )
    if team is not None:
        stmt = stmt.where(DailyRollup.team_id == team)
    rows = (await session.execute(stmt)).all()
    points = [
        schemas.VolumePoint(
            day=day, opened=int(opened or 0), closed=int(closed or 0), replies=int(replies or 0)
        )
        for day, opened, closed, replies in rows
    ]
    return schemas.VolumeReport(points=points)


async def responsiveness(
    session: AsyncSession,
    principal: Principal,
    *,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    team_id: str | None = None,
) -> schemas.ResponsivenessReport:
    """Median + p90 first-response seconds over conversations opened in the window."""
    authorize(principal, min_role=Role.AGENT)
    frm, to = _range(date_from, date_to)
    team = _team_uuid(team_id)
    col = ConversationMetric.first_response_s
    stmt = select(
        func.percentile_cont(0.5).within_group(col.asc()),
        func.percentile_cont(0.9).within_group(col.asc()),
        func.count(col),
    ).where(col.is_not(None), _opened_day() >= frm, _opened_day() <= to)
    if team is not None:
        stmt = stmt.where(ConversationMetric.team_id == team)
    median, p90, count = (await session.execute(stmt)).one()
    return schemas.ResponsivenessReport(
        first_response=schemas.FirstResponse(
            median_s=float(median) if median is not None else None,
            p90_s=float(p90) if p90 is not None else None,
            count=int(count),
        )
    )


async def csat(
    session: AsyncSession,
    principal: Principal,
    *,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    team_id: str | None = None,
) -> schemas.CsatReport:
    """CSAT count/average + 1-5 histogram, summed from ``daily_rollups`` (composes across days)."""
    authorize(principal, min_role=Role.AGENT)
    frm, to = _range(date_from, date_to)
    team = _team_uuid(team_id)
    stmt = select(
        DailyRollup.rating_count, DailyRollup.rating_sum, DailyRollup.rating_histogram
    ).where(DailyRollup.day >= frm, DailyRollup.day <= to)
    if team is not None:
        stmt = stmt.where(DailyRollup.team_id == team)
    rows = (await session.execute(stmt)).all()

    count = 0
    total = 0
    distribution = {str(star): 0 for star in range(1, 6)}
    for rating_count, rating_sum, histogram in rows:
        count += int(rating_count or 0)
        total += int(rating_sum or 0)
        for star, n in (histogram or {}).items():
            distribution[str(star)] = distribution.get(str(star), 0) + int(n)
    average = round(total / count, 3) if count else None
    return schemas.CsatReport(count=count, average=average, distribution=distribution)


async def queue(session: AsyncSession, principal: Principal) -> schemas.QueueReport:
    """Live queue monitor: open/unassigned counts + longest wait (conversation head) + agents online
    (Redis presence). Served from a short-TTL Redis cache so it is O(1) and refreshes ≤10 s."""
    authorize(principal, min_role=Role.AGENT)
    ws_pub = encode_public_id(IdPrefix.WORKSPACE, principal.workspace_id)
    redis = get_redis()
    cache_key = f"{QUEUE_CACHE_PREFIX}{ws_pub}"

    cached = await redis.get(cache_key)
    if cached is not None:
        return schemas.QueueReport.model_validate_json(cached)

    snapshot = await messaging_service.queue_snapshot(session)
    agents = await realtime.online_agents(ws_pub)
    report = schemas.QueueReport(
        open=int(snapshot["open"] or 0),
        unassigned=int(snapshot["unassigned"] or 0),
        longest_wait_s=snapshot["longest_wait_s"],
        agents_online=len(agents),
    )
    await redis.set(cache_key, report.model_dump_json(), ex=QUEUE_CACHE_TTL_SECONDS)
    return report


# --- Neko analytics (P1.4, RFC-003 §8) --------------------------------------------------------


async def neko(
    session: AsyncSession,
    principal: Principal,
    *,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
) -> schemas.NekoReport:
    """Neko analytics over the window from ``neko_daily_rollups`` (never scans raw ``agent_runs``):
    a per-day series (resolution/deflection/cost/latency over time) plus window totals with the
    handoff-reasons breakdown. Resolutions are the billing meter's net, so the numbers reconcile
    with Stripe (RFC-003 §8). Neko is not team-scoped, so there is no team filter."""
    authorize(principal, min_role=Role.AGENT)
    frm, to = _range(date_from, date_to)
    rows = (
        await session.scalars(
            select(NekoDailyRollup)
            .where(NekoDailyRollup.day >= frm, NekoDailyRollup.day <= to)
            .order_by(NekoDailyRollup.day)
        )
    ).all()

    points: list[schemas.NekoDailyPoint] = []
    reasons: dict[str, int] = {}
    engaged = answered = handoff = runs_total = 0
    resolutions = cost = latency_sum = 0.0
    latency_count = 0
    for r in rows:
        runs_total += r.runs_total
        engaged += r.conversations_engaged
        answered += r.conversations_answered
        handoff += r.conversations_handoff
        resolutions += float(r.resolutions)
        cost += r.cost_usd_sum
        latency_sum += r.latency_ms_sum
        latency_count += r.latency_count
        for reason, n in (r.handoff_reasons or {}).items():
            reasons[reason] = reasons.get(reason, 0) + int(n)
        points.append(
            schemas.NekoDailyPoint(
                day=r.day,
                runs_total=r.runs_total,
                runs_answered=r.runs_answered,
                runs_clarify=r.runs_clarify,
                runs_handoff=r.runs_handoff,
                runs_ineligible=r.runs_ineligible,
                runs_error=r.runs_error,
                conversations_engaged=r.conversations_engaged,
                conversations_answered=r.conversations_answered,
                conversations_handoff=r.conversations_handoff,
                resolutions=float(r.resolutions),
                cost_usd=round(r.cost_usd_sum, 6),
                avg_latency_ms=(
                    round(r.latency_ms_sum / r.latency_count, 1) if r.latency_count else None
                ),
            )
        )

    # ponytail: window ``engaged`` sums per-day distinct counts, so a conversation spanning two UTC
    # days is counted once per day — same approximation as the volume report's daily sums. Exact
    # conversation-distinct rates would need a windowed scan of agent_runs (what rollups avoid).
    totals = schemas.NekoTotals(
        runs_total=runs_total,
        conversations_engaged=engaged,
        conversations_answered=answered,
        conversations_handoff=handoff,
        resolutions=round(resolutions, 4),
        resolution_rate=_rate(resolutions, engaged),
        deflection_rate=_rate(engaged - handoff, engaged),
        cost_usd=round(cost, 6),
        avg_cost_per_conversation=(round(cost / engaged, 6) if engaged else None),
        avg_latency_ms=(round(latency_sum / latency_count, 1) if latency_count else None),
        handoff_reasons=dict(sorted(reasons.items(), key=lambda kv: kv[1], reverse=True)),
    )
    return schemas.NekoReport(points=points, totals=totals)


async def neko_csat(
    session: AsyncSession,
    principal: Principal,
    *,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
) -> schemas.NekoCsatReport:
    """CSAT delta (RFC-003 §8): average rating of Neko-touched conversations vs the rest, over
    ratings given in the window. Reads ``conversation_metrics`` (the projection, split by the
    ``ai_involved`` flag) directly — like ``responsiveness``, and still never a parts scan."""
    authorize(principal, min_role=Role.AGENT)
    frm, to = _range(date_from, date_to)
    stmt = (
        select(
            ConversationMetric.ai_involved,
            ConversationMetric.rating,
            func.count().label("n"),
        )
        .where(
            ConversationMetric.rating.is_not(None),
            _rated_day() >= frm,
            _rated_day() <= to,
        )
        .group_by(ConversationMetric.ai_involved, ConversationMetric.rating)
    )
    rows = (await session.execute(stmt)).all()

    buckets: dict[bool, dict[str, int]] = {True: {}, False: {}}
    for ai_involved, rating, n in rows:
        buckets[bool(ai_involved)][str(int(rating))] = int(n)

    def _bucket(hist: dict[str, int]) -> schemas.CsatBucket:
        distribution = {str(star): 0 for star in range(1, 6)}
        distribution.update(hist)
        count = sum(hist.values())
        total = sum(int(star) * n for star, n in hist.items())
        return schemas.CsatBucket(
            count=count,
            average=round(total / count, 3) if count else None,
            distribution=distribution,
        )

    neko_bucket = _bucket(buckets[True])
    other_bucket = _bucket(buckets[False])
    delta = (
        round(neko_bucket.average - other_bucket.average, 3)
        if neko_bucket.average is not None and other_bucket.average is not None
        else None
    )
    return schemas.NekoCsatReport(neko_touched=neko_bucket, non_neko=other_bucket, delta=delta)
