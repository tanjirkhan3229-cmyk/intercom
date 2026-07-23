"""Mid-flight token streaming to the widget (RFC-003 §5, RFC-001 §6.3).

Generation tokens stream **directly through Redis pub/sub → the gateway**, never through the durable
outbox: "tokens are never stored mid-flight, only the final part" (RFC-003 §5). So this path is
best-effort and ephemeral — a gateway hiccup must never fail the turn, and the authoritative record
is the persisted conversation_part (fanned out normally via the outbox once the turn commits).

If the verifier rejects the drafted answer, the pipeline emits a ``superseded`` signal so the widget
clears the streamed draft; the durable handoff/answer then arrives through the outbox. The publish
function is injectable so tests capture deltas without a live Centrifugo.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from relay.core import realtime
from relay.core.logging import get_logger

log = get_logger(__name__)

AI_STREAM_START = "ai.stream.start"
AI_STREAM_DELTA = "ai.stream.delta"
AI_STREAM_END = "ai.stream.end"
AI_STREAM_SUPERSEDED = "ai.stream.superseded"

Publish = Callable[[str, dict[str, Any]], Awaitable[None]]


async def _best_effort_publish(channel: str, data: dict[str, Any]) -> None:
    """Publish to Centrifugo, swallowing errors — a realtime blip must not fail the turn."""
    try:
        await realtime.publish(channel, data)
    except Exception as exc:
        log.warning("ai.stream.publish_failed", channel=channel, error=str(exc))


class TurnStream:
    """Streams one turn's deltas to a conversation's gateway channel (ephemeral)."""

    def __init__(self, conversation_public_id: str, *, publish: Publish | None = None) -> None:
        self._channel = realtime.conv_channel(conversation_public_id)
        self._conv = conversation_public_id
        self._publish = publish or _best_effort_publish
        self._run_id: str | None = None

    async def start(self, run_id: str) -> None:
        self._run_id = run_id
        await self._emit(AI_STREAM_START, {})

    async def delta(self, text: str) -> None:
        await self._emit(AI_STREAM_DELTA, {"delta": text})

    async def end(self, part_id: str | None) -> None:
        await self._emit(AI_STREAM_END, {"part_id": part_id})

    async def superseded(self, reason: str) -> None:
        """The drafted answer was rejected/handed off — tell the widget to drop the draft."""
        await self._emit(AI_STREAM_SUPERSEDED, {"reason": reason})

    async def _emit(self, topic: str, extra: dict[str, Any]) -> None:
        await self._publish(
            self._channel,
            {"topic": topic, "conversation_id": self._conv, "run_id": self._run_id, **extra},
        )
