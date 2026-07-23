"""Unit test for the knowledge-indexing consumer's outbox->task routing + dedupe (P1.1)."""

from __future__ import annotations

import json

from relay.core.outbox import OUTBOX_STREAM
from relay.modules.knowledge import events
from relay.modules.knowledge.indexing_consumer import _task_for, consume_once


def test_task_for_maps_topics() -> None:
    assert _task_for(events.ARTICLE_PUBLISHED) == "knowledge.reindex_article"
    assert _task_for(events.ARTICLE_UPDATED) == "knowledge.reindex_article"
    assert _task_for(events.ARTICLE_UNPUBLISHED) == "knowledge.deindex_article"
    assert _task_for(events.ARTICLE_DELETED) == "knowledge.deindex_article"
    assert _task_for("conversation.created") is None  # other groups own it


class FakeRedis:
    """Just enough of the Redis stream API for consume_once."""

    def __init__(self, entries: list[tuple[str, dict[str, str]]]) -> None:
        self._entries = entries
        self.marks: set[str] = set()
        self.acked: list[str] = []

    async def xreadgroup(self, group, consumer, streams, count, block=None):
        if streams.get(OUTBOX_STREAM) == ">" and self._entries:
            batch, self._entries = self._entries, []
            return [(OUTBOX_STREAM, batch)]
        return []

    async def exists(self, key: str) -> int:
        return 1 if key in self.marks else 0

    async def set(self, key: str, _val: str, ex: int | None = None) -> None:
        self.marks.add(key)

    async def xack(self, _stream: str, _group: str, entry_id: str) -> None:
        self.acked.append(entry_id)


def _entry(topic: str, ws: str, article_id: str, outbox_id: str = "ob1"):
    return (
        "1-0",
        {
            "topic": topic,
            "outbox_id": outbox_id,
            "aggregate_id": article_id,
            "payload": json.dumps({"workspace_id": ws}),
        },
    )


async def test_consume_enqueues_reindex_and_acks() -> None:
    ws, aid = "ws-uuid", "art-uuid"
    redis = FakeRedis([_entry(events.ARTICLE_PUBLISHED, ws, aid)])
    calls: list[tuple[str, str, str]] = []
    read = await consume_once(redis, lambda t, w, a: calls.append((t, w, a)), from_id=">")
    assert read == 1
    assert calls == [("knowledge.reindex_article", ws, aid)]
    assert redis.acked == ["1-0"]


async def test_consume_dedupes_already_done() -> None:
    redis = FakeRedis([_entry(events.ARTICLE_PUBLISHED, "ws", "art", outbox_id="dup")])
    redis.marks.add("kb:index:done:dup")  # already processed
    calls: list[tuple[str, str, str]] = []
    await consume_once(redis, lambda t, w, a: calls.append((t, w, a)), from_id=">")
    assert calls == []  # skipped
    assert redis.acked == ["1-0"]  # but still acked


async def test_consume_ignores_unrelated_topic() -> None:
    redis = FakeRedis([_entry("conversation.created", "ws", "cnv")])
    calls: list[tuple[str, str, str]] = []
    await consume_once(redis, lambda t, w, a: calls.append((t, w, a)), from_id=">")
    assert calls == [] and redis.acked == ["1-0"]
