"""Unit tests for the pure reporting reducer (P0.9 acceptance 1: metrics reconcile against a
hand-computed fixture set). No database — the reducer folds event payloads deterministically.
"""

from __future__ import annotations

import datetime as dt
import uuid

from relay.core.ids import IdPrefix, encode_public_id
from relay.modules.messaging import events
from relay.modules.reporting.reducer import Metrics, apply_event, fold

T0 = dt.datetime(2026, 7, 1, 12, 0, 0, tzinfo=dt.UTC)


def _at(seconds: int) -> str:
    return (T0 + dt.timedelta(seconds=seconds)).isoformat()


# Fixed ids for the fixture (encoded to the public base62 form the real payloads carry).
WS = uuid.uuid4()
CNV = uuid.uuid4()
CONTACT = uuid.uuid4()
TEAM = uuid.uuid4()
ADMIN = uuid.uuid4()

WS_PUB = encode_public_id(IdPrefix.WORKSPACE, WS)
CNV_PUB = encode_public_id(IdPrefix.CONVERSATION, CNV)
TEAM_PUB = encode_public_id(IdPrefix.TEAM, TEAM)
ADMIN_PUB = encode_public_id(IdPrefix.ADMIN, ADMIN)


def _conv_payload(
    *, occurred_s: int, state: str = "open", team: str | None = None, assignee: str | None = None
) -> dict[str, object]:
    return {
        "workspace_id": WS_PUB,
        "conversation_id": CNV_PUB,
        "contact_id": encode_public_id(IdPrefix.CONTACT, CONTACT),
        "state": state,
        "team_id": team,
        "assignee_id": assignee,
        "occurred_at": _at(occurred_s),
    }


def _part_payload(
    *, at_s: int, part_type: str, author_kind: str, rating: int | None = None, **conv: object
) -> dict[str, object]:
    payload = _conv_payload(occurred_s=at_s, **conv)  # type: ignore[arg-type]
    payload.update(
        part_id=encode_public_id(IdPrefix.PART, uuid.uuid4()),
        part_type=part_type,
        author_kind=author_kind,
        created_at=_at(at_s),
    )
    if rating is not None:
        payload["rating"] = rating
    return payload


def _lifecycle() -> list[tuple[str, dict[str, object], int]]:
    """A full conversation: open → contact msg → 2 agent replies → rating 5 → close."""
    return [
        (events.CONVERSATION_CREATED, _conv_payload(occurred_s=0, team=TEAM_PUB), 1),
        # opening contact comment — not an agent reply, no first_response yet
        (
            events.CONVERSATION_PART_CREATED,
            _part_payload(at_s=0, part_type="comment", author_kind="contact", team=TEAM_PUB),
            2,
        ),
        # first agent reply at +30s → first_response_s = 30, replies_count = 1
        (
            events.CONVERSATION_PART_CREATED,
            _part_payload(
                at_s=30, part_type="comment", author_kind="admin", assignee=ADMIN_PUB, team=TEAM_PUB
            ),
            3,
        ),
        # second agent reply at +90s → replies_count = 2, first_response unchanged
        (
            events.CONVERSATION_PART_CREATED,
            _part_payload(
                at_s=90, part_type="comment", author_kind="admin", assignee=ADMIN_PUB, team=TEAM_PUB
            ),
            4,
        ),
        # contact rating 5 at +300s
        (
            events.CONVERSATION_PART_CREATED,
            _part_payload(
                at_s=300,
                part_type="rating",
                author_kind="contact",
                rating=5,
                assignee=ADMIN_PUB,
                team=TEAM_PUB,
            ),
            5,
        ),
        # close at +600s → resolution_s = 600
        (
            events.CONVERSATION_STATE_CHANGED,
            {
                **_conv_payload(occurred_s=600, state="closed", assignee=ADMIN_PUB, team=TEAM_PUB),
                "from": "open",
                "to": "closed",
            },
            6,
        ),
    ]


def test_full_lifecycle_reconciles_to_hand_computed_values() -> None:
    m = fold(_lifecycle())

    assert m.workspace_id == WS
    assert m.conversation_id == CNV
    assert m.team_id == TEAM
    assert m.assignee_id == ADMIN
    assert m.opened_at == T0
    assert m.first_admin_reply_at == T0 + dt.timedelta(seconds=30)
    assert m.first_response_s == 30
    assert m.replies_count == 2
    assert m.rating == 5
    assert m.rated_at == T0 + dt.timedelta(seconds=300)
    assert m.closed_at == T0 + dt.timedelta(seconds=600)
    assert m.resolution_s == 600
    assert m.reopen_count == 0
    assert m.last_seq == 6


def test_replay_is_idempotent() -> None:
    """Re-folding the same event stream (at-least-once redelivery) yields identical metrics, and
    re-applying an already-seen seq is a no-op."""
    seq = _lifecycle()
    once = fold(seq)
    # Replay every event again on top — seq guard makes each a no-op.
    twice = once
    for topic, payload, s in seq:
        twice = apply_event(twice, topic, payload, s)
    assert twice == once


def test_apply_event_does_not_mutate_input() -> None:
    base = Metrics()
    after = apply_event(base, events.CONVERSATION_CREATED, _conv_payload(occurred_s=0), 1)
    assert base.opened_at is None  # input untouched
    assert after.opened_at == T0
    assert after is not base


def test_reopen_clears_resolution() -> None:
    seq = _lifecycle()
    reopen = {**_conv_payload(occurred_s=900, state="open", team=TEAM_PUB)}
    reopen.update({"from": "closed", "to": "open"})
    seq.append((events.CONVERSATION_STATE_CHANGED, reopen, 7))
    m = fold(seq)
    assert m.reopen_count == 1
    assert m.closed_at is None
    assert m.resolution_s is None
    # A reply after reopen still counts and first_response stays the original.
    assert m.first_response_s == 30


def test_first_response_uses_earliest_agent_reply_only() -> None:
    seq = [
        (events.CONVERSATION_CREATED, _conv_payload(occurred_s=0), 1),
        (
            events.CONVERSATION_PART_CREATED,
            _part_payload(at_s=10, part_type="comment", author_kind="ai_agent"),
            2,
        ),
        (
            events.CONVERSATION_PART_CREATED,
            _part_payload(at_s=50, part_type="comment", author_kind="admin"),
            3,
        ),
    ]
    m = fold(seq)
    assert m.first_response_s == 10  # ai_agent counts as an agent reply
    assert m.replies_count == 2


def test_team_latched_on_first_observed_team() -> None:
    """Opened team-less then routed to a team: team_id latches to the first team observed, and a
    subsequent reassignment must NOT change it (immutable after first non-null)."""
    other_pub = encode_public_id(IdPrefix.TEAM, uuid.uuid4())
    seq = [
        (events.CONVERSATION_CREATED, _conv_payload(occurred_s=0, team=None), 1),
        (
            events.CONVERSATION_PART_CREATED,
            _part_payload(at_s=0, part_type="comment", author_kind="contact"),
            2,
        ),
        (events.CONVERSATION_ASSIGNED, _conv_payload(occurred_s=30, team=TEAM_PUB), 3),
        (events.CONVERSATION_ASSIGNED, _conv_payload(occurred_s=60, team=other_pub), 4),
    ]
    m = fold(seq)
    assert m.team_id == TEAM  # first observed team; the later reassignment did not move it


def test_team_stays_none_when_never_assigned() -> None:
    seq = [
        (events.CONVERSATION_CREATED, _conv_payload(occurred_s=0, team=None), 1),
        (
            events.CONVERSATION_PART_CREATED,
            _part_payload(at_s=5, part_type="comment", author_kind="admin"),
            2,
        ),
    ]
    assert fold(seq).team_id is None
