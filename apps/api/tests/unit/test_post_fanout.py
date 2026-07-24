"""Unit test: in-app post delivery events route to the recipient contact's own channel."""

from __future__ import annotations

from relay.core.realtime_fanout import channels_for_event


def test_post_delivered_routes_to_contact_channel() -> None:
    channels = channels_for_event(
        "outbound.post.delivered",
        {"workspace_id": "wrk_a", "contact_id": "usr_b", "post_id": "pst_c"},
    )
    assert channels == ["contact:usr_b"]


def test_post_without_contact_is_dropped() -> None:
    assert channels_for_event("outbound.post.delivered", {"workspace_id": "wrk_a"}) == []


def test_conversation_event_still_routes_to_conv_and_inbox() -> None:
    channels = channels_for_event(
        "conversation.part.created", {"workspace_id": "wrk_a", "conversation_id": "cnv_x"}
    )
    assert "conv:cnv_x" in channels
    assert any(c.startswith("inbox:") for c in channels)
