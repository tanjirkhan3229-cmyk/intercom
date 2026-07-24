"""Workflow graph schema + validation (P1.5, RFC-001 §6.7).

A workflow *version* stores a graph as JSON: a set of typed nodes plus the entry (its single
``trigger`` node). This module is the **zod-style server validator** (the P1.5 prompt) — a graph is
validated at publish time and the executor then trusts the stored structure. Validation is strict:
unknown node types/actions, dangling references, orphan (unreachable) nodes, missing required
params, and malformed condition predicates are all rejected with a 422 + a ``path`` pointing at the
offending node/field.

Node types::

    trigger    one per graph, the entry. {trigger: <key>, filter?: <predicate>, next: <id>}
    condition  {predicate: <predicate>, "true": <id>, "false": <id>}
    action     {action: <type>, params: {...}, next: <id>}
    bot_step   {bot: ask_buttons|collect|disambiguate, params: {...}}  (branch targets in params)
    wait       {params: {seconds:int>0 | until:iso}, next: <id>}
    end        terminal

**Invariant (documented):** each node executes at most once per run — enforced downstream by the
``workflow_run_steps`` UNIQUE ``(run_id, node_id)`` ledger. This guarantees termination (a run
visits each node ≤ once), so back-edges/loops are out of scope for P1.5; the graph may reference an
earlier node, but the executor will treat a re-visit as already-done and skip it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from relay.core.errors import ValidationError
from relay.core.predicates import validate_predicate

# --- Vocabulary ---------------------------------------------------------------

NODE_TYPES: Final[frozenset[str]] = frozenset(
    {"trigger", "condition", "action", "bot_step", "wait", "end"}
)

# Trigger keys with a real outbox event source wired in P1.5 (see events.OUTBOX_TO_TRIGGER).
SOURCED_TRIGGER_KEYS: Final[frozenset[str]] = frozenset(
    {
        "conversation.created",
        "contact.message.created",
        "contact.created",
        "contact.updated",
        "conversation.state_changed",
    }
)
# Framework-ready trigger types whose event source lands with their owning work (beat scheduler,
# inbound endpoint, …). Accepted by validation so a graph can be authored now, but a workflow using
# one will simply never fire until its source exists (surfaced by ``service.publish``).
FRAMEWORK_TRIGGER_KEYS: Final[frozenset[str]] = frozenset(
    {"schedule", "webhook_in", "admin.action", "attribute.changed"}
)
TRIGGER_KEYS: Final[frozenset[str]] = SOURCED_TRIGGER_KEYS | FRAMEWORK_TRIGGER_KEYS

ACTION_TYPES: Final[frozenset[str]] = frozenset(
    {
        "assign",
        "route_to_team",
        "add_tag",
        "set_attribute",
        "snooze",
        "close",
        "send_reply",
        "hand_to_aide",
        "call_webhook",
        "apply_sla",
    }
)
BOT_KINDS: Final[frozenset[str]] = frozenset({"ask_buttons", "collect", "disambiguate"})
_ATTR_TARGETS: Final[frozenset[str]] = frozenset({"conversation", "contact"})

_MAX_NODES: Final[int] = 200  # a sane ceiling; a hostile/degenerate graph is rejected


# --- Parsed representation ----------------------------------------------------


@dataclass(frozen=True)
class WorkflowGraph:
    """A validated graph. The executor reads ``nodes[node_id]`` (raw validated dicts) and follows
    edges via :func:`outgoing`."""

    nodes: dict[str, dict[str, Any]]
    entry: str
    trigger_key: str

    def get(self, node_id: str) -> dict[str, Any] | None:
        return self.nodes.get(node_id)

    @classmethod
    def from_dict(cls, graph: Any) -> WorkflowGraph:
        """Validate ``graph`` (publish path) and build; raises ``ValidationError`` on any
        problem."""
        validate_graph(graph)  # raises on any problem
        return cls.load(graph)

    @classmethod
    def load(cls, graph: dict[str, Any]) -> WorkflowGraph:
        """Build from an already-validated stored graph (the executor's hot path — no revalidation).
        A version's graph was validated at publish time and versions are immutable, so revalidating
        on every advance would be wasted work. Raises ``ValueError`` (not ``StopIteration``) if a
        graph somehow has no trigger node, so a corrupt row degrades to a handled error."""
        nodes = {n["id"]: n for n in graph["nodes"]}
        trigger = next((n for n in graph["nodes"] if n["type"] == "trigger"), None)
        if trigger is None:
            raise ValueError("workflow graph has no trigger node")
        return cls(nodes=nodes, entry=trigger["id"], trigger_key=trigger["trigger"])


def outgoing(node: Mapping[str, Any]) -> list[str]:
    """The node-id targets a node can transition to (for reachability + execution)."""
    ntype = node.get("type")
    if ntype in ("trigger", "action", "wait"):
        nxt = node.get("next")
        return [nxt] if isinstance(nxt, str) else []
    if ntype == "condition":
        return [t for t in (node.get("true"), node.get("false")) if isinstance(t, str)]
    if ntype == "bot_step":
        return _bot_targets(node.get("params") or {})
    return []  # end


def _bot_targets(params: Mapping[str, Any]) -> list[str]:
    targets: list[str] = []
    for opt in params.get("options") or []:
        if isinstance(opt, Mapping) and isinstance(opt.get("next"), str):
            targets.append(opt["next"])
    for key in ("next", "default_next"):
        if isinstance(params.get(key), str):
            targets.append(params[key])
    return targets


# --- Validation ---------------------------------------------------------------


def _require(condition: bool, message: str, path: str) -> None:
    if not condition:
        raise ValidationError(message, details={"path": path})


def _is_str(v: Any) -> bool:
    return isinstance(v, str) and bool(v)


def validate_graph(graph: Any) -> None:
    """Raise :class:`ValidationError` unless ``graph`` is a well-formed, fully-connected workflow.

    Checks (in order): shape, node id uniqueness, exactly one trigger, per-node structure + params,
    every referenced target exists, and every node is reachable from the trigger (no orphans).
    """
    _require(isinstance(graph, Mapping), "graph must be an object", "graph")
    nodes = graph.get("nodes")
    _require(
        isinstance(nodes, list) and len(nodes) > 0,
        "graph.nodes must be a non-empty list",
        "graph.nodes",
    )
    _require(
        len(nodes) <= _MAX_NODES, f"graph has too many nodes (max {_MAX_NODES})", "graph.nodes"
    )

    ids: set[str] = set()
    triggers: list[str] = []
    for i, node in enumerate(nodes):
        path = f"graph.nodes[{i}]"
        _require(isinstance(node, Mapping), "node must be an object", path)
        nid = node.get("id")
        _require(_is_str(nid), "node needs a non-empty string 'id'", f"{path}.id")
        _require(nid not in ids, f"duplicate node id {nid!r}", f"{path}.id")
        ids.add(nid)
        ntype = node.get("type")
        _require(ntype in NODE_TYPES, f"unknown node type {ntype!r}", f"{path}.type")
        if ntype == "trigger":
            triggers.append(nid)

    _require(len(triggers) == 1, "graph must have exactly one 'trigger' node", "graph.nodes")

    # Per-node structural + param validation, and collect edges.
    for node in nodes:
        _validate_node(node, ids)

    # Reachability from the trigger (no orphans).
    entry = triggers[0]
    reachable: set[str] = set()
    stack = [entry]
    node_by_id = {n["id"]: n for n in nodes}
    while stack:
        cur = stack.pop()
        if cur in reachable:
            continue
        reachable.add(cur)
        stack.extend(outgoing(node_by_id[cur]))
    orphans = ids - reachable
    _require(
        not orphans,
        f"unreachable node(s): {', '.join(sorted(orphans))}",
        "graph.nodes",
    )

    # Acyclic: the engine runs each node at most once per run (the workflow_run_steps ledger), so a
    # cycle could strand a run (a back-edge into a fired wait/bot node would re-park it with no
    # pending timer/input). Loops/re-ask are out of scope for P1.5; reject them at publish so the
    # "each node once ⇒ termination" invariant is guaranteed, not merely documented.
    _require_acyclic(node_by_id, entry)


def _require_acyclic(node_by_id: dict[str, dict[str, Any]], entry: str) -> None:
    """DFS 3-colour cycle detection over the reachable graph."""
    WHITE, GREY, BLACK = 0, 1, 2
    colour: dict[str, int] = {}

    # Iterative DFS with an explicit stack of (node, child-index) frames (no recursion-depth limit).
    stack: list[tuple[str, list[str], int]] = [(entry, outgoing(node_by_id[entry]), 0)]
    colour[entry] = GREY
    while stack:
        nid, children, i = stack[-1]
        if i == len(children):
            colour[nid] = BLACK
            stack.pop()
            continue
        stack[-1] = (nid, children, i + 1)
        child = children[i]
        c = colour.get(child, WHITE)
        if c == GREY:
            raise ValidationError(
                f"graph has a cycle through node {child!r} (loops are not supported)",
                details={"path": f"node[{child}]"},
            )
        if c == WHITE:
            colour[child] = GREY
            stack.append((child, outgoing(node_by_id[child]), 0))


def _validate_node(node: Mapping[str, Any], ids: set[str]) -> None:
    nid = node["id"]
    path = f"node[{nid}]"
    ntype = node["type"]

    def edge(name: str, value: Any) -> None:
        _require(_is_str(value), f"{ntype} node needs a '{name}' target", f"{path}.{name}")
        _require(value in ids, f"{name!r} points at unknown node {value!r}", f"{path}.{name}")

    if ntype == "trigger":
        _require(
            node.get("trigger") in TRIGGER_KEYS,
            f"unknown trigger {node.get('trigger')!r}",
            f"{path}.trigger",
        )
        if "filter" in node and node["filter"] is not None:
            validate_predicate(node["filter"], _path=f"{path}.filter")
        edge("next", node.get("next"))
    elif ntype == "condition":
        _require("predicate" in node, "condition needs a 'predicate'", f"{path}.predicate")
        validate_predicate(node["predicate"], _path=f"{path}.predicate")
        edge("true", node.get("true"))
        edge("false", node.get("false"))
    elif ntype == "action":
        action = node.get("action")
        _require(action in ACTION_TYPES, f"unknown action {action!r}", f"{path}.action")
        assert isinstance(action, str)  # narrowed by the _require above (ACTION_TYPES are str)
        _validate_action_params(action, node.get("params") or {}, path)
        edge("next", node.get("next"))
    elif ntype == "bot_step":
        _validate_bot(node, ids, path)
    elif ntype == "wait":
        _validate_wait_params(node.get("params") or {}, path)
        edge("next", node.get("next"))
    # end: nothing to validate


def _validate_action_params(action: str, params: Mapping[str, Any], path: str) -> None:
    p = f"{path}.params"
    _require(isinstance(params, Mapping), "params must be an object", p)
    if action == "assign":
        _require(
            _is_str(params.get("assignee_id")) or _is_str(params.get("team_id")),
            "assign requires 'assignee_id' and/or 'team_id'",
            p,
        )
    elif action == "route_to_team":
        _require(_is_str(params.get("team_id")), "route_to_team requires 'team_id'", p)
    elif action == "add_tag":
        _require(_is_str(params.get("name")), "add_tag requires a 'name'", p)
    elif action == "set_attribute":
        _require(
            params.get("target") in _ATTR_TARGETS,
            "set_attribute 'target' must be 'conversation' or 'contact'",
            p,
        )
        _require(_is_str(params.get("key")), "set_attribute requires a 'key'", p)
        _require("value" in params, "set_attribute requires a 'value'", p)
    elif action == "snooze":
        _validate_wait_params(params, path)  # same duration shape
    elif action == "send_reply":
        _require(_is_str(params.get("body")), "send_reply requires a 'body'", p)
    elif action == "call_webhook":
        _require(_is_str(params.get("url")), "call_webhook requires a 'url'", p)
        # P1.5 delivers via the SSRF-guarded POST path (like webhook delivery); other verbs land
        # with the app framework (P2.9). ``headers`` (if present) must be a string→string map.
        _require(
            params.get("method", "POST") == "POST", "call_webhook only supports POST in P1.5", p
        )
        headers = params.get("headers")
        _require(
            headers is None
            or (
                isinstance(headers, dict)
                and all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items())
            ),
            "call_webhook 'headers' must be a string→string object",
            p,
        )
    # close / hand_to_aide: no required params. apply_sla: registered (flag-gated), params free.


def _validate_wait_params(params: Mapping[str, Any], path: str) -> None:
    p = f"{path}.params"
    _require(isinstance(params, Mapping), "params must be an object", p)
    seconds = params.get("seconds")
    until = params.get("until")
    if seconds is not None:
        _require(
            isinstance(seconds, int) and not isinstance(seconds, bool) and seconds > 0,
            "'seconds' must be a positive integer",
            p,
        )
    elif until is not None:
        _require(_is_str(until), "'until' must be an ISO-8601 string", p)
    else:
        _require(False, "wait/snooze requires 'seconds' or 'until'", p)


def _validate_bot(node: Mapping[str, Any], ids: set[str], path: str) -> None:
    bot = node.get("bot")
    _require(bot in BOT_KINDS, f"unknown bot kind {bot!r}", f"{path}.bot")
    params = node.get("params") or {}
    p = f"{path}.params"
    _require(isinstance(params, Mapping), "params must be an object", p)
    _require(_is_str(params.get("prompt")), f"{bot} requires a 'prompt'", p)

    def target(name: str, value: Any) -> None:
        _require(_is_str(value), f"bot option needs a '{name}' target", p)
        _require(value in ids, f"{name!r} points at unknown node {value!r}", p)

    if bot in ("ask_buttons", "disambiguate"):
        options = params.get("options")
        _require(isinstance(options, list) and len(options) >= 1, f"{bot} requires 'options'", p)
        assert isinstance(options, list)  # narrowed by the _require above
        seen_values: set[str] = set()
        for opt in options:
            _require(isinstance(opt, Mapping), "each option must be an object", p)
            _require(_is_str(opt.get("label")), "option needs a 'label'", p)
            _require(_is_str(opt.get("value")), "option needs a 'value'", p)
            _require(
                opt["value"] not in seen_values, f"duplicate option value {opt.get('value')!r}", p
            )
            seen_values.add(opt["value"])
            target("next", opt.get("next"))
        if "default_next" in params and params["default_next"] is not None:
            target("default_next", params["default_next"])
    else:  # collect
        _require(
            params.get("target") in _ATTR_TARGETS,
            "collect 'target' must be 'conversation' or 'contact'",
            p,
        )
        _require(_is_str(params.get("key")), "collect requires a 'key' to store the reply", p)
        target("next", params.get("next"))
