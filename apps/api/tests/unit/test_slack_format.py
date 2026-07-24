"""Unit tests for Slack notification formatting + Zapier allowlist contract (no DB)."""

from __future__ import annotations

from relay.core.public_api import _route_allowed
from relay.modules.integrations.slack_format import format_notification


def test_format_conversation_created() -> None:
    text = format_notification(
        "conversation.created", {"conversation_id": "cnv_1", "channel": "email"}
    )
    assert "New conversation" in text and "cnv_1" in text and "email" in text


def test_format_part_created() -> None:
    text = format_notification("conversation.part.created", {"conversation_id": "cnv_9"})
    assert "reply" in text.lower() and "cnv_9" in text


def test_zapier_routes_are_api_key_allowlisted() -> None:
    assert _route_allowed("GET", "/v0/zapier/auth/test")
    assert _route_allowed("POST", "/v0/zapier/subscriptions")
    assert _route_allowed("DELETE", "/v0/zapier/subscriptions/whk_abc")
    # Admin integration config must NOT be reachable by an API key.
    assert not _route_allowed("POST", "/v0/integrations/slack")
    assert not _route_allowed("GET", "/v0/integrations")
