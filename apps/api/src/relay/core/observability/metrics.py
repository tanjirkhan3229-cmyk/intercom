"""Prometheus metrics — the four golden signals per runtime shape (RFC-001 §9).

Golden signals: **latency** (HTTP + task duration histograms), **errors** (status-labelled
counters), **traffic** (request/task counters), **saturation** (outbox backlog + oldest-age
gauges, DB-pool checkouts). Metric names are stable; labels are deliberately LOW-cardinality —
route *templates* (never raw paths with ids), task names, queue names — never a ``workspace_id``
or a raw id, so the series count stays bounded at the RFC-000 §4 envelope (1-5k workspaces).

Exposition: the `app` serves ``GET /metrics`` (see ``relay.health``); non-HTTP shapes
(worker/beat/relay/fanout) call ``start_metrics_server()``. When ``PROMETHEUS_MULTIPROC_DIR`` is
set (prod prefork/uvicorn), a fresh multiprocess registry is assembled per scrape.
"""

from __future__ import annotations

import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
    start_http_server,
)
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from relay import __version__
from relay.core.logging import get_logger
from relay.settings import get_settings

log = get_logger(__name__)

# --- HTTP (the `app` shape) -------------------------------------------------------------------
# NB: prometheus_client appends ``_total`` to a Counter's sample name, so the base name omits it
# (``relay_http_requests`` → exposition ``relay_http_requests_total``).
HTTP_REQUESTS = Counter(
    "relay_http_requests",
    "HTTP requests handled by the app.",
    ["method", "route", "status"],
)
HTTP_LATENCY = Histogram(
    "relay_http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["method", "route"],
)

# --- Celery (the `workers`/`beat` shapes) -----------------------------------------------------
CELERY_TASKS = Counter(
    "relay_celery_tasks",
    "Celery tasks processed, by terminal state.",
    ["task", "queue", "status"],
)
CELERY_LATENCY = Histogram(
    "relay_celery_task_duration_seconds",
    "Celery task execution time in seconds.",
    ["task", "queue"],
)

# --- Outbox relay (the `relay` shape) — saturation --------------------------------------------
OUTBOX_PENDING = Gauge(
    "relay_outbox_pending_rows",
    "Unpublished outbox rows (queue depth).",
    multiprocess_mode="livemostrecent",
)
OUTBOX_OLDEST_AGE = Gauge(
    "relay_outbox_oldest_age_seconds",
    "Age of the oldest unpublished outbox row, in seconds (0 when empty).",
    multiprocess_mode="livemostrecent",
)

# --- DB pool (app/worker) — saturation --------------------------------------------------------
DB_POOL_IN_USE = Gauge(
    "relay_db_pool_in_use_connections",
    "Checked-out connections on the writer pool.",
    multiprocess_mode="livemostrecent",
)

# --- Build / deploy marker (RFC-001 §13) ------------------------------------------------------
# livemostrecent (like the gauges above) so it collapses to a single pid-less series under the
# multiprocess collector — a deploy marker must be one constant series, not one per worker pid.
BUILD_INFO = Gauge(
    "relay_build_info",
    "Build/deploy marker; value is always 1, the deploy_sha label changes per release.",
    ["version", "deploy_sha", "environment"],
    multiprocess_mode="livemostrecent",
)

_UNMATCHED_ROUTE = "__unmatched__"
# task_id -> perf_counter start, so postrun can compute duration without re-timing. Bounded: a
# task_postrun signal is normally guaranteed, but a hard worker kill (SIGKILL/OOM) skips it, so
# the map is capped to stop a missed-postrun path from leaking entries without bound.
_TASK_STARTS: dict[str, float] = {}
_TASK_STARTS_MAX = 10_000


def _route_label(request: Request) -> str:
    """Templated route path (``/v0/contacts/{contact_id}``), never the raw path — cardinality.

    Starlette stores the matched route on the scope during routing; after ``call_next`` it is
    available. Unmatched requests (404) collapse to a single ``__unmatched__`` series.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else _UNMATCHED_ROUTE


class MetricsMiddleware(BaseHTTPMiddleware):
    """Time every request and record traffic/latency/errors. Placed just inside CORS so preflight
    OPTIONS handled by CORS aren't metered, but everything the app actually routes is."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        start = time.perf_counter()
        method = request.method
        try:
            response = await call_next(request)
        except Exception:
            # Unhandled error propagating to ServerErrorMiddleware — record a 500 then re-raise.
            route = _route_label(request)
            HTTP_REQUESTS.labels(method=method, route=route, status="500").inc()
            HTTP_LATENCY.labels(method=method, route=route).observe(time.perf_counter() - start)
            raise
        route = _route_label(request)
        HTTP_REQUESTS.labels(method=method, route=route, status=str(response.status_code)).inc()
        HTTP_LATENCY.labels(method=method, route=route).observe(time.perf_counter() - start)
        return response


# --- Celery signal handlers -------------------------------------------------------------------
def _queue_of(task: Any) -> str:
    info = getattr(getattr(task, "request", None), "delivery_info", None) or {}
    routing_key = info.get("routing_key")
    return routing_key if isinstance(routing_key, str) and routing_key else "unknown"


def _on_task_prerun(task_id: str | None = None, task: Any = None, **_: Any) -> None:
    if task_id is None:
        return
    # Defensive bound: if postrun was skipped for earlier tasks (hard kill), evict the oldest half
    # rather than let the map grow without limit.
    if len(_TASK_STARTS) >= _TASK_STARTS_MAX:
        for stale in list(_TASK_STARTS)[: _TASK_STARTS_MAX // 2]:
            _TASK_STARTS.pop(stale, None)
    _TASK_STARTS[task_id] = time.perf_counter()


def _on_task_postrun(
    task_id: str | None = None, task: Any = None, state: str | None = None, **_: Any
) -> None:
    name = getattr(task, "name", "unknown")
    queue = _queue_of(task)
    start = _TASK_STARTS.pop(task_id, None) if task_id is not None else None
    if start is not None:
        CELERY_LATENCY.labels(task=name, queue=queue).observe(time.perf_counter() - start)
    # A retried attempt fires postrun with state RETRY; it is counted once by _on_task_retry as
    # status="retry", so we must NOT also count it as "failure" — keeps success/failure/retry
    # disjoint (matches docs/observability.md + the failure-ratio alert semantics).
    if state == "RETRY":
        return
    status = "success" if state == "SUCCESS" else "failure"
    CELERY_TASKS.labels(task=name, queue=queue, status=status).inc()


def _on_task_retry(request: Any = None, sender: Any = None, **_: Any) -> None:
    name = getattr(sender, "name", None) or getattr(request, "task", None) or "unknown"
    CELERY_TASKS.labels(task=str(name), queue="unknown", status="retry").inc()


_metrics_registered = False


def register_celery_metrics() -> None:
    """Connect task lifecycle signals so worker/beat processes emit task metrics. Idempotent —
    on_after_configure can fire more than once, and re-connecting would double-count every task."""
    global _metrics_registered
    if _metrics_registered:
        return
    from celery.signals import task_postrun, task_prerun, task_retry

    # dispatch_uid makes connect() idempotent even if the guard is bypassed.
    task_prerun.connect(_on_task_prerun, weak=False, dispatch_uid="relay_metrics_prerun")
    task_postrun.connect(_on_task_postrun, weak=False, dispatch_uid="relay_metrics_postrun")
    task_retry.connect(_on_task_retry, weak=False, dispatch_uid="relay_metrics_retry")
    _metrics_registered = True


# --- Runtime gauges (refreshed on scrape) -----------------------------------------------------
def set_build_info() -> None:
    s = get_settings()
    BUILD_INFO.labels(version=__version__, deploy_sha=s.deploy_sha, environment=s.environment).set(
        1
    )


def refresh_db_pool_gauge() -> None:
    """Best-effort writer-pool checkout count for the process serving the scrape. Never raises.

    Accurate for the deployed shape — one Uvicorn worker per ECS task, scaled horizontally — where
    each task is a single process with its own pool. Under a prefork/multi-worker app (not used
    here), aggregate DB saturation would instead need each worker to refresh its own multiproc file.
    """
    try:
        from relay.core.db import get_engine

        pool = get_engine().sync_engine.pool
        checked_out = pool.checkedout()  # type: ignore[attr-defined]
        DB_POOL_IN_USE.set(float(checked_out))
    except Exception:  # pragma: no cover - defensive
        pass


def refresh_runtime_gauges() -> None:
    """Called by the /metrics handler right before rendering."""
    set_build_info()
    refresh_db_pool_gauge()


def render_latest() -> tuple[bytes, str]:
    """Render exposition text. Uses a multiprocess registry when ``PROMETHEUS_MULTIPROC_DIR`` is
    set (prefork/uvicorn), else the default process-global registry."""
    mp_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if mp_dir:
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
        return generate_latest(registry), CONTENT_TYPE_LATEST
    return generate_latest(), CONTENT_TYPE_LATEST


_server_started = False


def start_metrics_server() -> None:
    """Start a Prometheus scrape server for a non-HTTP shape (worker/beat/relay/fanout).

    Idempotent and best-effort: a bind failure is logged, never fatal. In multiprocess mode the
    server is started once in the parent; children write to ``PROMETHEUS_MULTIPROC_DIR``.
    """
    global _server_started
    settings = get_settings()
    if not settings.metrics_enabled or _server_started:
        return
    try:
        if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
            registry = CollectorRegistry()
            multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
            start_http_server(settings.metrics_port, registry=registry)
        else:
            start_http_server(settings.metrics_port)
        _server_started = True
        log.info("metrics.server.started", port=settings.metrics_port)
    except OSError as exc:  # pragma: no cover - environment dependent
        log.warning("metrics.server.failed", port=settings.metrics_port, error=str(exc))
