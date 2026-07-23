"""Prometheus metrics unit tests (P0.12, RFC-001 §9).

No containers: the app factory, /metrics endpoint, and Celery signal handlers all work without a
DB/Redis connection. Asserts golden-signal series exist, labels are low-cardinality route
templates, and /metrics stays out of the OpenAPI/SDK contract.
"""

from __future__ import annotations

from typing import ClassVar

import httpx
from prometheus_client import REGISTRY

from relay.core.observability import metrics as m
from relay.main import create_app


def _sample(name: str, labels: dict[str, str]) -> float | None:
    return REGISTRY.get_sample_value(name, labels)


async def test_http_metrics_recorded_and_exposed() -> None:
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    labels = {"method": "GET", "route": "/v0/hello", "status": "200"}
    before = _sample("relay_http_requests_total", labels) or 0.0

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        hello = await c.get("/v0/hello")
        assert hello.status_code == 200
        metrics_resp = await c.get("/metrics")

    after = _sample("relay_http_requests_total", labels) or 0.0
    assert after == before + 1  # exactly one /v0/hello request counted

    assert metrics_resp.status_code == 200
    body = metrics_resp.text
    assert "relay_http_requests_total" in body
    assert "relay_http_request_duration_seconds" in body
    assert 'route="/v0/hello"' in body  # templated route label, not a raw path
    assert "relay_build_info" in body  # deploy marker present


def test_route_label_unmatched() -> None:
    class _Req:
        scope: ClassVar[dict[str, object]] = {}

    assert m._route_label(_Req()) == "__unmatched__"  # type: ignore[arg-type]


def test_celery_signal_handlers_record_traffic_and_latency() -> None:
    class _FakeRequest:
        delivery_info: ClassVar[dict[str, str]] = {"routing_key": "analytics"}

    class _FakeTask:
        name = "crm.drain_events"
        request = _FakeRequest()

    task = _FakeTask()
    labels = {"task": "crm.drain_events", "queue": "analytics", "status": "success"}
    before = _sample("relay_celery_tasks_total", labels) or 0.0

    m._on_task_prerun(task_id="task-1", task=task)
    m._on_task_postrun(task_id="task-1", task=task, state="SUCCESS")

    after = _sample("relay_celery_tasks_total", labels) or 0.0
    assert after == before + 1
    duration_count = _sample(
        "relay_celery_task_duration_seconds_count",
        {"task": "crm.drain_events", "queue": "analytics"},
    )
    assert duration_count is not None and duration_count >= 1.0


def test_failed_task_labelled_failure() -> None:
    class _FakeTask:
        name = "messaging.purge_idempotency_keys"
        request = type("R", (), {"delivery_info": {"routing_key": "housekeeping"}})()

    labels = {
        "task": "messaging.purge_idempotency_keys",
        "queue": "housekeeping",
        "status": "failure",
    }
    before = _sample("relay_celery_tasks_total", labels) or 0.0
    m._on_task_prerun(task_id="task-2", task=_FakeTask())
    m._on_task_postrun(task_id="task-2", task=_FakeTask(), state="FAILURE")
    after = _sample("relay_celery_tasks_total", labels) or 0.0
    assert after == before + 1


def test_retry_attempt_not_counted_as_failure() -> None:
    # A retried attempt fires postrun with state=RETRY; it must NOT increment status="failure"
    # (it is counted once as status="retry"), so success/failure/retry stay disjoint.
    class _FakeTask:
        name = "channels.poll_domains"
        request = type("R", (), {"delivery_info": {"routing_key": "housekeeping"}})()

    fail_labels = {"task": "channels.poll_domains", "queue": "housekeeping", "status": "failure"}
    before = _sample("relay_celery_tasks_total", fail_labels) or 0.0
    m._on_task_prerun(task_id="retry-1", task=_FakeTask())
    m._on_task_postrun(task_id="retry-1", task=_FakeTask(), state="RETRY")
    after = _sample("relay_celery_tasks_total", fail_labels) or 0.0
    assert after == before  # RETRY did not bump the failure counter


def test_metrics_excluded_from_openapi_contract() -> None:
    app = create_app()
    assert "/metrics" not in app.openapi()["paths"]
