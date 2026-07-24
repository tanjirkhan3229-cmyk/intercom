"""Unit tests for workflow graph validation (automation/graph.py)."""

from __future__ import annotations

import pytest

from relay.core.errors import ValidationError
from relay.modules.automation.graph import WorkflowGraph, outgoing, validate_graph


def _wrap(*nodes: dict) -> dict:
    return {"nodes": list(nodes)}


VALID = _wrap(
    {"id": "t", "type": "trigger", "trigger": "conversation.created", "next": "c"},
    {
        "id": "c",
        "type": "condition",
        "predicate": {"op": "eq", "field": "state", "value": "open"},
        "true": "a",
        "false": "e",
    },
    {"id": "a", "type": "action", "action": "add_tag", "params": {"name": "vip"}, "next": "e"},
    {"id": "e", "type": "end"},
)


def test_valid_graph_parses() -> None:
    validate_graph(VALID)
    g = WorkflowGraph.from_dict(VALID)
    assert g.entry == "t"
    assert g.trigger_key == "conversation.created"
    assert set(g.nodes) == {"t", "c", "a", "e"}
    assert outgoing(g.nodes["c"]) == ["a", "e"]
    assert outgoing(g.nodes["e"]) == []


def test_bot_and_wait_targets() -> None:
    g = _wrap(
        {"id": "t", "type": "trigger", "trigger": "conversation.created", "next": "b"},
        {
            "id": "b",
            "type": "bot_step",
            "bot": "ask_buttons",
            "params": {
                "prompt": "?",
                "options": [
                    {"label": "Yes", "value": "y", "next": "w"},
                    {"label": "No", "value": "n", "next": "e"},
                ],
            },
        },
        {"id": "w", "type": "wait", "params": {"seconds": 60}, "next": "e"},
        {"id": "e", "type": "end"},
    )
    validate_graph(g)
    assert set(outgoing(g["nodes"][1])) == {"w", "e"}  # bot options


@pytest.mark.parametrize(
    "graph",
    [
        "notadict",
        {"nodes": []},
        {"nodes": [{"id": "e", "type": "end"}]},  # no trigger
        _wrap(  # two triggers
            {"id": "t1", "type": "trigger", "trigger": "conversation.created", "next": "e"},
            {"id": "t2", "type": "trigger", "trigger": "contact.created", "next": "e"},
            {"id": "e", "type": "end"},
        ),
        _wrap(  # duplicate id
            {"id": "t", "type": "trigger", "trigger": "conversation.created", "next": "e"},
            {"id": "t", "type": "end"},
        ),
        _wrap(  # unknown node type
            {"id": "t", "type": "trigger", "trigger": "conversation.created", "next": "x"},
            {"id": "x", "type": "frobnicate"},
        ),
        _wrap(  # unknown trigger key
            {"id": "t", "type": "trigger", "trigger": "made.up", "next": "e"},
            {"id": "e", "type": "end"},
        ),
        _wrap(  # dangling next
            {"id": "t", "type": "trigger", "trigger": "conversation.created", "next": "ghost"},
            {"id": "e", "type": "end"},
        ),
        _wrap(  # orphan node (unreachable)
            {"id": "t", "type": "trigger", "trigger": "conversation.created", "next": "e"},
            {"id": "e", "type": "end"},
            {"id": "orphan", "type": "action", "action": "close", "params": {}, "next": "e"},
        ),
        _wrap(  # unknown action
            {"id": "t", "type": "trigger", "trigger": "conversation.created", "next": "a"},
            {"id": "a", "type": "action", "action": "nuke", "params": {}, "next": "e"},
            {"id": "e", "type": "end"},
        ),
        _wrap(  # action missing required params
            {"id": "t", "type": "trigger", "trigger": "conversation.created", "next": "a"},
            {"id": "a", "type": "action", "action": "add_tag", "params": {}, "next": "e"},
            {"id": "e", "type": "end"},
        ),
        _wrap(  # bad condition predicate
            {"id": "t", "type": "trigger", "trigger": "conversation.created", "next": "c"},
            {
                "id": "c",
                "type": "condition",
                "predicate": {"op": "bogus"},
                "true": "e",
                "false": "e",
            },
            {"id": "e", "type": "end"},
        ),
        _wrap(  # wait without duration
            {"id": "t", "type": "trigger", "trigger": "conversation.created", "next": "w"},
            {"id": "w", "type": "wait", "params": {}, "next": "e"},
            {"id": "e", "type": "end"},
        ),
        _wrap(  # call_webhook non-POST
            {"id": "t", "type": "trigger", "trigger": "conversation.created", "next": "a"},
            {
                "id": "a",
                "type": "action",
                "action": "call_webhook",
                "params": {"url": "https://x.test/h", "method": "GET"},
                "next": "e",
            },
            {"id": "e", "type": "end"},
        ),
        _wrap(  # bot with duplicate option values
            {"id": "t", "type": "trigger", "trigger": "conversation.created", "next": "b"},
            {
                "id": "b",
                "type": "bot_step",
                "bot": "ask_buttons",
                "params": {
                    "prompt": "?",
                    "options": [
                        {"label": "A", "value": "x", "next": "e"},
                        {"label": "B", "value": "x", "next": "e"},
                    ],
                },
            },
            {"id": "e", "type": "end"},
        ),
    ],
)
def test_rejects_invalid_graphs(graph: object) -> None:
    with pytest.raises(ValidationError):
        validate_graph(graph)


def test_rejects_cycle() -> None:
    # a → b → a is a cycle (loops are out of scope for P1.5; the engine runs each node once).
    cyclic = _wrap(
        {"id": "t", "type": "trigger", "trigger": "conversation.created", "next": "a"},
        {"id": "a", "type": "action", "action": "close", "params": {}, "next": "b"},
        {"id": "b", "type": "action", "action": "close", "params": {}, "next": "a"},
    )
    with pytest.raises(ValidationError):
        validate_graph(cyclic)


def test_diamond_dag_is_valid() -> None:
    # A node reachable by two paths (diamond) is a DAG, not a cycle — must be accepted.
    diamond = _wrap(
        {"id": "t", "type": "trigger", "trigger": "conversation.created", "next": "c"},
        {
            "id": "c",
            "type": "condition",
            "predicate": {"op": "exists", "field": "x"},
            "true": "a1",
            "false": "a2",
        },
        {"id": "a1", "type": "action", "action": "close", "params": {}, "next": "e"},
        {"id": "a2", "type": "action", "action": "close", "params": {}, "next": "e"},
        {"id": "e", "type": "end"},
    )
    validate_graph(diamond)  # must not raise


def test_set_attribute_and_call_webhook_valid() -> None:
    g = _wrap(
        {"id": "t", "type": "trigger", "trigger": "contact.created", "next": "a1"},
        {
            "id": "a1",
            "type": "action",
            "action": "set_attribute",
            "params": {"target": "contact", "key": "tier", "value": "gold"},
            "next": "a2",
        },
        {
            "id": "a2",
            "type": "action",
            "action": "call_webhook",
            "params": {"url": "https://x.test/hook"},
            "next": "e",
        },
        {"id": "e", "type": "end"},
    )
    validate_graph(g)  # must not raise
