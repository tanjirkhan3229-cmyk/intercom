"""Unit tests for the channels Celery task wrappers — exception→disposition mapping (P0.7).

Exercises the ACTUAL worker entrypoints' branching (terminal ack vs transient retry vs DLQ) with a
fake ``self`` and stubbed service coroutines, so the classification that ships is asserted without
DB/event-loop coupling. Covers the review finding that ``tasks.py`` was untested."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from relay.modules.channels import service, tasks


class _Retry(Exception):
    """Stand-in for celery's Retry, raised by the fake ``self.retry``."""


class _FakeSelf:
    def __init__(self, retries: int, max_retries: int) -> None:
        self.max_retries = max_retries
        self.request = SimpleNamespace(retries=retries)

    def retry(self, *, exc: BaseException, countdown: int) -> None:
        raise _Retry


def _invoke(task: Any, self_obj: _FakeSelf, **kwargs: Any) -> Any:
    """Call a bound celery task's underlying function with a fake ``self`` (bypass the worker)."""
    return task.run.__func__(self_obj, **kwargs)


def _ids() -> dict[str, str]:
    return {
        "workspace_id": str(uuid.uuid4()),
        "conversation_id": str(uuid.uuid4()),
        "part_id": str(uuid.uuid4()),
    }


# --- ingest_email dispositions ------------------------------------------------


def test_ingest_unroutable_returns_dlq_and_records(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[dict[str, Any]] = []

    async def fake_ingest(**_kw: Any) -> str:
        raise service.UnroutableEmail("no route")

    async def fake_record(**kw: Any) -> None:
        recorded.append(dict(kw))

    monkeypatch.setattr(service, "ingest", fake_ingest)
    monkeypatch.setattr(tasks, "_record_failure", fake_record)
    result = _invoke(
        tasks.ingest_email, _FakeSelf(0, 5), sns_message_id="m1", s3_bucket="b", s3_key="k"
    )
    assert result == "dlq"
    assert len(recorded) == 1  # DLQ row written, task acked (no poison redelivery)


def test_ingest_transient_under_max_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_ingest(**_kw: Any) -> str:
        raise RuntimeError("db blip")

    monkeypatch.setattr(service, "ingest", fake_ingest)
    with pytest.raises(_Retry):  # a transient error must retry, never ack-as-failure
        _invoke(tasks.ingest_email, _FakeSelf(0, 5), sns_message_id="m", s3_bucket="b", s3_key="k")


def test_ingest_transient_exhausted_returns_dlq(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[dict[str, Any]] = []

    async def fake_ingest(**_kw: Any) -> str:
        raise RuntimeError("db blip")

    async def fake_record(**kw: Any) -> None:
        recorded.append(dict(kw))

    monkeypatch.setattr(service, "ingest", fake_ingest)
    monkeypatch.setattr(tasks, "_record_failure", fake_record)
    result = _invoke(
        tasks.ingest_email, _FakeSelf(5, 5), sns_message_id="m", s3_bucket="b", s3_key="k"
    )
    assert result == "dlq"
    assert len(recorded) == 1


def test_ingest_success_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_ingest(**_kw: Any) -> str:
        return "ingested"

    monkeypatch.setattr(service, "ingest", fake_ingest)
    assert (
        _invoke(tasks.ingest_email, _FakeSelf(0, 5), sns_message_id="m", s3_bucket="b", s3_key="k")
        == "ingested"
    )


# --- send_email dispositions --------------------------------------------------


def test_send_suppressed_is_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_send(**_kw: Any) -> str:
        raise service.SuppressedRecipient("suppressed")

    monkeypatch.setattr(service, "send_email", fake_send)
    assert _invoke(tasks.send_email, _FakeSelf(0, 8), **_ids()) == "blocked_suppressed"


def test_send_too_large_is_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_send(**_kw: Any) -> str:
        raise service.MessageTooLarge("big")

    monkeypatch.setattr(service, "send_email", fake_send)
    assert _invoke(tasks.send_email, _FakeSelf(0, 8), **_ids()) == "blocked_too_large"


def test_send_transient_under_max_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_send(**_kw: Any) -> str:
        raise RuntimeError("redis down")

    monkeypatch.setattr(service, "send_email", fake_send)
    with pytest.raises(_Retry):  # a must-not-lose reply retries, never silently acked
        _invoke(tasks.send_email, _FakeSelf(0, 8), **_ids())


def test_send_transient_exhausted_returns_dlq(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[dict[str, Any]] = []

    async def fake_send(**_kw: Any) -> str:
        raise RuntimeError("redis down")

    async def fake_record(**kw: Any) -> None:
        recorded.append(dict(kw))

    monkeypatch.setattr(service, "send_email", fake_send)
    monkeypatch.setattr(tasks, "_record_send_failure", fake_record)
    assert _invoke(tasks.send_email, _FakeSelf(8, 8), **_ids()) == "dlq"
    assert len(recorded) == 1


def test_send_success_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_send(**_kw: Any) -> str:
        return "sent"

    monkeypatch.setattr(service, "send_email", fake_send)
    assert _invoke(tasks.send_email, _FakeSelf(0, 8), **_ids()) == "sent"
