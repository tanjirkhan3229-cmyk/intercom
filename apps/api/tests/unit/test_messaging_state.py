"""Unit test for the conversation state-machine matrix (RFC-002 §5.3). No DB."""

from __future__ import annotations

from relay.modules.messaging.service import VALID_TRANSITIONS


def test_transition_matrix() -> None:
    # Legal transitions.
    assert "snoozed" in VALID_TRANSITIONS["open"]
    assert "closed" in VALID_TRANSITIONS["open"]
    assert "open" in VALID_TRANSITIONS["snoozed"]
    assert "closed" in VALID_TRANSITIONS["snoozed"]
    assert "open" in VALID_TRANSITIONS["closed"]  # reopen

    # Illegal transitions.
    assert "snoozed" not in VALID_TRANSITIONS["closed"]  # can't snooze a closed convo
    assert "open" not in VALID_TRANSITIONS["open"]  # no self-loop
    assert "snoozed" not in VALID_TRANSITIONS["snoozed"]

    # Every state is known and every target is a real state.
    states = {"open", "snoozed", "closed"}
    assert set(VALID_TRANSITIONS) == states
    for targets in VALID_TRANSITIONS.values():
        assert targets <= states
