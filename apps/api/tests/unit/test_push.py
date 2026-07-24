"""Unit tests for the mobile push adapters + consumer filter (P1.10). No DB/Redis."""

from __future__ import annotations

import pytest

from relay.modules.messaging import push, push_consumer


def _msg(token: str = "tok", platform: str = "ios") -> push.PushMessage:
    return push.PushMessage(
        platform=platform,
        token=token,
        title="t",
        body="b",
        topic="com.example.app",
        environment="production",
        data={"conversation_id": "cnv_1"},
    )


# --- FakePusher ---------------------------------------------------------------


def test_fake_pusher_captures_and_can_force_failures() -> None:
    fake = push.FakePusher()
    fake.fail_next = 1
    with pytest.raises(push.PushSendError):
        fake.send(_msg())
    # After the forced failure is consumed, the next send is captured.
    mid = fake.send(_msg("tok-2"))
    assert mid == "fake-1"
    assert [m.token for m in fake.sent] == ["tok-2"]


def test_fake_pusher_reports_invalid_tokens() -> None:
    fake = push.FakePusher()
    fake.invalid.add("dead")
    with pytest.raises(push.PushTokenInvalid):
        fake.send(_msg("dead"))
    assert fake.sent == []


# --- CircuitBreaker -----------------------------------------------------------


def test_breaker_opens_after_threshold_transient_failures() -> None:
    fake = push.FakePusher()
    breaker = push.CircuitBreaker(fake, threshold=2, cooldown=100.0)
    fake.fail_next = 5
    for _ in range(2):
        with pytest.raises(push.PushSendError):
            breaker.send(_msg())
    # Now open: the call is short-circuited (the fake would still fail, but we never reach it).
    with pytest.raises(push.PushSendError, match="circuit breaker open"):
        breaker.send(_msg())


def test_breaker_does_not_count_dead_tokens() -> None:
    fake = push.FakePusher()
    fake.invalid.add("dead")
    breaker = push.CircuitBreaker(fake, threshold=1, cooldown=100.0)
    # A dead token is a token problem, not provider health — it must not trip the breaker.
    for _ in range(3):
        with pytest.raises(push.PushTokenInvalid):
            breaker.send(_msg("dead"))
    # A subsequent good send goes through (breaker still closed).
    assert breaker.send(_msg("ok")) == "fake-1"


# --- PushDispatcher -----------------------------------------------------------


def test_dispatcher_routes_by_platform() -> None:
    ios, android = push.FakePusher(), push.FakePusher()
    dispatcher = push.PushDispatcher(ios=ios, android=android)
    dispatcher.send(_msg("a", platform="ios"))
    dispatcher.send(_msg("b", platform="android"))
    assert [m.token for m in ios.sent] == ["a"]
    assert [m.token for m in android.sent] == ["b"]


def test_dispatcher_rejects_unknown_platform() -> None:
    dispatcher = push.PushDispatcher(ios=push.FakePusher(), android=push.FakePusher())
    with pytest.raises(push.PushSendError):
        dispatcher.send(_msg(platform="web"))


# --- Consumer filter ----------------------------------------------------------


@pytest.mark.parametrize(
    ("author_kind", "part_type", "expected"),
    [
        ("admin", "comment", True),
        ("ai_agent", "comment", True),
        ("contact", "comment", False),  # never push a contact their own message
        ("admin", "note", False),  # internal note → no push
        ("system", "state_change", False),
    ],
)
def test_should_push(author_kind: str, part_type: str, expected: bool) -> None:
    payload = {"author_kind": author_kind, "part_type": part_type}
    assert push_consumer._should_push("conversation.part.created", payload) is expected


def test_should_push_ignores_other_topics() -> None:
    payload = {"author_kind": "admin", "part_type": "comment"}
    assert push_consumer._should_push("conversation.assigned", payload) is False
