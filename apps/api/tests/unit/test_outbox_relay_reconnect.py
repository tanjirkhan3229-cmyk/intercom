"""Outbox relay survives connectivity loss (RFC-001 §9: Postgres failover / Redis blip).

Regression tests for the P0.12 chaos finding + the principal-review follow-ups: the relay must
reconnect (not crash) on transient Postgres AND Redis errors, must step aside on the FIRST attempt
if a genuine peer holds the advisory lock, and must RETRY (not permanently exit) if the lock is
contended after it has already been running (its own not-yet-reaped ghost). Deterministic/offline:
``psycopg.connect``, ``time.sleep`` and ``random.random`` are faked so no DB/Redis is touched.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
import redis.exceptions

import relay.core.outbox_relay as r


class _Result:
    def __init__(self, lock: bool) -> None:
        self._lock = lock

    def fetchone(self) -> tuple[bool]:
        return (self._lock,)


class _FakeCtl:
    """Stands in for the autocommit control connection: advisory-lock + LISTEN + notifies."""

    def __init__(self, lock: bool) -> None:
        self._lock = lock

    def execute(self, *_a: Any, **_k: Any) -> _Result:
        return _Result(self._lock)

    def notifies(self, *_a: Any, **_k: Any) -> list[Any]:
        return []

    def __enter__(self) -> _FakeCtl:
        return self

    def __exit__(self, *_a: Any) -> bool:
        return False


class _FakeSettings:
    database_url_psycopg = "postgresql://x/relay"
    metrics_enabled = False


@pytest.fixture(autouse=True)
def _no_jitter_no_delay(monkeypatch: Any) -> None:
    # random()==1.0 makes equal-jitter sleep == backoff exactly (deterministic); sleep is captured.
    monkeypatch.setattr(r.random, "random", lambda: 1.0)
    monkeypatch.setattr(r, "get_settings", lambda: _FakeSettings())


def _capture_sleeps(monkeypatch: Any) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr(r.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


def test_reconnects_on_postgres_operational_error(monkeypatch: Any) -> None:
    sleeps = _capture_sleeps(monkeypatch)
    calls = {"n": 0}

    def fake_connect(_dsn: str, autocommit: bool = False) -> _FakeCtl:
        calls["n"] += 1
        if calls["n"] == 1:
            raise psycopg.OperationalError("server closed the connection unexpectedly")
        return _FakeCtl(lock=False)  # reconnect: no peer, first acquire failed → clean return

    monkeypatch.setattr(r.psycopg, "connect", fake_connect)
    r.run_relay()
    assert calls["n"] == 2
    assert sleeps == [r._RECONNECT_BACKOFF_START]


def test_reconnects_on_redis_connection_error(monkeypatch: Any) -> None:
    # A Redis blip must be survived too — it is not a psycopg error.
    sleeps = _capture_sleeps(monkeypatch)
    calls = {"n": 0}

    def fake_connect(_dsn: str, autocommit: bool = False) -> _FakeCtl:
        calls["n"] += 1
        if calls["n"] == 1:
            raise redis.exceptions.ConnectionError("redis down")
        return _FakeCtl(lock=False)

    monkeypatch.setattr(r.psycopg, "connect", fake_connect)
    r.run_relay()
    assert calls["n"] == 2
    assert sleeps == [r._RECONNECT_BACKOFF_START]


def test_exits_when_peer_holds_lock_on_first_attempt(monkeypatch: Any) -> None:
    # Never acquired → a held lock means a genuine peer; step aside cleanly with no retry.
    sleeps = _capture_sleeps(monkeypatch)
    calls = {"n": 0}

    def fake_connect(_dsn: str, autocommit: bool = False) -> _FakeCtl:
        calls["n"] += 1
        return _FakeCtl(lock=False)

    monkeypatch.setattr(r.psycopg, "connect", fake_connect)
    r.run_relay()
    assert calls["n"] == 1
    assert sleeps == []


def test_retries_contended_lock_after_running(monkeypatch: Any) -> None:
    # Acquired once, then the work connection drops; on reconnect the lock is briefly held by our
    # own ghost — the relay must RETRY, not permanently exit. A sentinel stops the test.
    sleeps = _capture_sleeps(monkeypatch)
    seq: list[bool] = []

    def fake_connect(_dsn: str, autocommit: bool = False) -> _FakeCtl:
        seq.append(autocommit)
        n = len(seq)
        if autocommit:  # control connection
            if n == 1:
                return _FakeCtl(lock=True)  # acquire the lock
            if n == 3:
                return _FakeCtl(lock=False)  # contended after we already ran (ghost)
            raise RuntimeError("sentinel-stop")  # 4th ctl connect ends the test
        raise psycopg.OperationalError("work connection dropped after acquire")  # work connect

    monkeypatch.setattr(r.psycopg, "connect", fake_connect)
    with pytest.raises(RuntimeError, match="sentinel-stop"):
        r.run_relay()
    # ctl connects at n=1 (acquire), n=3 (contended-retry), n=4 (sentinel); work connect at n=2.
    assert seq == [True, False, True, True]
    # one sleep for the work-conn drop, one for the contended-lock retry.
    assert len(sleeps) == 2
