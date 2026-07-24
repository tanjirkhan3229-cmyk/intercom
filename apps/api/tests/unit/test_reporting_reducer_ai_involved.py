"""Unit: the reducer folds ``ai_involved`` (RFC-002 §5.6) — the CSAT-delta signal (P1.4).

Pure, DB-free (mirrors test_reporting_reducer.py): a conversation is Neko-touched once Neko authors
ANY part (an answer comment or a handoff note), and stays untouched when only humans/contacts act.
"""

from __future__ import annotations

import uuid

from relay.core.ids import IdPrefix, encode_public_id
from relay.modules.messaging import events
from relay.modules.reporting.reducer import fold

WS = encode_public_id(IdPrefix.WORKSPACE, uuid.UUID(int=1))
CNV = encode_public_id(IdPrefix.CONVERSATION, uuid.UUID(int=2))


def _created(seq: int, *, author_kind: str, part_type: str = "comment") -> tuple:
    return (
        events.CONVERSATION_PART_CREATED,
        {
            "workspace_id": WS,
            "conversation_id": CNV,
            "author_kind": author_kind,
            "part_type": part_type,
            "created_at": "2026-07-24T10:00:00+00:00",
        },
        seq,
    )


def test_ai_agent_reply_marks_conversation_neko_touched() -> None:
    m = fold([_created(1, author_kind="contact"), _created(2, author_kind="ai_agent")])
    assert m.ai_involved is True


def test_ai_agent_handoff_note_also_marks_touched() -> None:
    # A handoff posts a private summary NOTE (not a comment) — still Neko-touched.
    m = fold([_created(1, author_kind="ai_agent", part_type="note")])
    assert m.ai_involved is True


def test_human_only_conversation_is_not_neko_touched() -> None:
    m = fold([_created(1, author_kind="contact"), _created(2, author_kind="admin")])
    assert m.ai_involved is False
