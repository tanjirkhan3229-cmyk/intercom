"""Pure formatting of a Relay conversation event into a Slack message (P1.9).

The outbox payload carries no message body (RFC-001 §6.5 keeps events small), so a v0 notification
is a concise notice — event kind + the conversation's public id + channel — not a transcript. Kept
pure so it is unit-tested without a DB or Slack.
"""

from __future__ import annotations

from typing import Any


def format_notification(topic: str, payload: dict[str, Any]) -> str:
    """Return the Slack message text for a subscribable conversation event."""
    conversation = payload.get("conversation_id", "?")
    channel = payload.get("channel", "chat")
    if topic == "conversation.created":
        return f":inbox_tray: New conversation ({channel}) — {conversation}"
    if topic == "conversation.part.created":
        return f":speech_balloon: New customer reply — {conversation}"
    return f"Conversation update ({topic}) — {conversation}"
