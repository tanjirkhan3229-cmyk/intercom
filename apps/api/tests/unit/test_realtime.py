"""Unit tests for the realtime edge (RFC-001 §6.3): channels, tokens, fan-out mapping.

The widget-token scoping test is the P0.4 acceptance in miniature — a widget token's channel
allow-list is exactly its own conversation, so it can never subscribe to another one.
"""

from __future__ import annotations

from relay.core import realtime, realtime_fanout
from relay.core.ids import IdPrefix, encode_public_id, uuid7


def test_channel_builders() -> None:
    assert realtime.conv_channel("cnv_1") == "conv:cnv_1"
    assert realtime.inbox_channel("wrk_1", realtime.INBOX_ALL) == "inbox:wrk_1:all"
    assert realtime.inbox_channel("wrk_1", "team_9") == "inbox:wrk_1:team_9"


def test_agent_connection_token_is_identity_only() -> None:
    admin_id, workspace_id = uuid7(), uuid7()
    token = realtime.agent_connection_token(
        admin_id=admin_id, workspace_id=workspace_id, role="agent"
    )
    claims = realtime.decode_centrifugo_token(token)
    assert claims["sub"] == str(admin_id)
    # Agents carry no channels claim — every channel is authorised per-subscription.
    assert "channels" not in claims
    assert claims["info"]["kind"] == "agent"
    assert claims["info"]["ws"] == encode_public_id(IdPrefix.WORKSPACE, workspace_id)


def test_subscription_token_names_exactly_one_channel() -> None:
    token = realtime.mint_subscription_token("adm-sub", "conv:cnv_1")
    claims = realtime.decode_centrifugo_token(token)
    assert claims["sub"] == "adm-sub"
    assert claims["channel"] == "conv:cnv_1"


def test_widget_token_pinned_to_its_own_conversation() -> None:
    workspace_id, contact_id = uuid7(), uuid7()
    conv_a, conv_b = uuid7(), uuid7()
    token = realtime.widget_connection_token(
        workspace_id=workspace_id, contact_id=contact_id, conversation_id=conv_a
    )
    claims = realtime.decode_centrifugo_token(token)

    conv_a_channel = realtime.conv_channel(encode_public_id(IdPrefix.CONVERSATION, conv_a))
    conv_b_channel = realtime.conv_channel(encode_public_id(IdPrefix.CONVERSATION, conv_b))
    contact_feed = realtime.contact_channel(encode_public_id(IdPrefix.CONTACT, contact_id))
    # The allow-list is exactly its own conversation + its own contact feed (P1.8 in-app posts);
    # Centrifugo refuses anything else — notably another conversation.
    assert claims["channels"] == [conv_a_channel, contact_feed]
    assert conv_b_channel not in claims["channels"]
    assert claims["info"]["kind"] == "contact"


def test_channels_for_event_fans_out_conv_and_inbox() -> None:
    payload = {"conversation_id": "cnv_1", "workspace_id": "wrk_1", "team_id": "team_9"}
    channels = realtime_fanout.channels_for_event("conversation.part.created", payload)
    assert channels == ["conv:cnv_1", "inbox:wrk_1:all", "inbox:wrk_1:team_9"]


def test_channels_for_event_unassigned_uses_none_bucket() -> None:
    payload = {"conversation_id": "cnv_2", "workspace_id": "wrk_1", "team_id": None}
    channels = realtime_fanout.channels_for_event("conversation.created", payload)
    assert channels == ["conv:cnv_2", "inbox:wrk_1:all", "inbox:wrk_1:none"]


def test_channels_for_event_ignores_non_conversation_topics() -> None:
    # Defensive: the consumer only invokes this for conversation.* topics, but the mapping should
    # still only ever emit channels derived from the payload it was given.
    payload = {"conversation_id": "cnv_3", "workspace_id": "wrk_2", "team_id": "team_1"}
    channels = realtime_fanout.channels_for_event("conversation.assigned", payload)
    assert all(ch.startswith(("conv:cnv_3", "inbox:wrk_2:")) for ch in channels)
