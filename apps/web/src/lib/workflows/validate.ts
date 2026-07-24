/**
 * Client-side graph validation — a faithful mirror of
 * `apps/api/src/relay/modules/automation/graph.py:validate_graph`, adapted to **collect all
 * problems** (the Python raises on the first) so the builder can surface every issue at once and
 * block publish. The server re-validates on publish and is the authoritative gate; this is the fast
 * pre-flight that keeps an invalid graph from ever reaching it (P1.6 acceptance #2).
 */

import {
  ACTION_TYPES,
  ATTR_TARGETS,
  BOT_KINDS,
  MAX_NODES,
  NODE_TYPES,
  TRIGGER_KEYS,
  type GraphError,
  type WorkflowGraph,
  type WorkflowNode,
} from "./contract";
import { predicateErrors } from "./predicate";
import { hasCycle, reachableFrom } from "./graph-utils";

const ACTIONS = new Set<string>(ACTION_TYPES);
const BOTS = new Set<string>(BOT_KINDS);
const TARGETS = new Set<string>(ATTR_TARGETS);
const TRIGGERS = new Set<string>(TRIGGER_KEYS);
const TYPES = new Set<string>(NODE_TYPES);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

/** Validate a full graph. Returns every problem found (empty array ⇒ publishable). */
export function validateGraph(graph: unknown): GraphError[] {
  const errors: GraphError[] = [];
  const err = (code: string, message: string, extra: Partial<GraphError> = {}) =>
    errors.push({ code, message, severity: "error", ...extra });

  if (!isRecord(graph) || !Array.isArray(graph.nodes)) {
    err("graph.shape", "Workflow graph must have a list of nodes.");
    return errors;
  }
  const nodes = graph.nodes as unknown[];
  if (nodes.length === 0) {
    err("graph.empty", "A workflow needs at least a trigger node.");
    return errors;
  }
  if (nodes.length > MAX_NODES) {
    err("graph.too_many_nodes", `A workflow can have at most ${MAX_NODES} nodes.`);
  }

  // Pass 1: ids, types, trigger count.
  const ids = new Set<string>();
  let triggerCount = 0;
  for (let i = 0; i < nodes.length; i++) {
    const node = nodes[i];
    const path = `graph.nodes[${i}]`;
    if (!isRecord(node)) {
      err("node.shape", "Each node must be an object.", { path });
      continue;
    }
    const id = node.id;
    if (!isNonEmptyString(id)) {
      err("node.id_missing", "A node is missing an id.", { path: `${path}.id` });
      continue;
    }
    if (ids.has(id)) {
      err("node.duplicate_id", `Duplicate node id "${id}".`, { nodeId: id, path: `${path}.id` });
    }
    ids.add(id);
    if (!isNonEmptyString(node.type) || !TYPES.has(node.type)) {
      err("node.unknown_type", `Unknown node type ${JSON.stringify(node.type)}.`, {
        nodeId: id,
        path: `${path}.type`,
      });
      continue;
    }
    if (node.type === "trigger") triggerCount += 1;
  }

  if (triggerCount !== 1) {
    err(
      "graph.trigger_count",
      triggerCount === 0
        ? "A workflow must start with exactly one trigger."
        : "A workflow can only have one trigger.",
    );
  }

  // Pass 2: per-node structure + params (only well-formed nodes).
  for (const raw of nodes) {
    if (!isRecord(raw) || !isNonEmptyString(raw.id) || !isNonEmptyString(raw.type)) continue;
    if (!TYPES.has(raw.type)) continue;
    validateNode(raw as unknown as WorkflowNode, ids, errors);
  }

  // Pass 3: reachability (orphans) + cycle warning — only meaningful with a valid single trigger.
  if (triggerCount === 1) {
    const validGraph = graph as unknown as WorkflowGraph;
    const reachable = reachableFrom(validGraph);
    for (const id of ids) {
      if (!reachable.has(id)) {
        err("node.unreachable", "This node can't be reached from the trigger.", { nodeId: id });
      }
    }
    if (hasCycle(validGraph)) {
      // graph.py `_require_acyclic` rejects loops at publish (a back-edge could strand a run), so
      // this is a blocking error, not a warning.
      errors.push({
        code: "graph.cycle",
        message: "Workflows can't loop — remove the back-edge (loops aren't supported).",
        severity: "error",
      });
    }
  }

  return errors;
}

function validateNode(node: WorkflowNode, ids: Set<string>, errors: GraphError[]): void {
  const rec = node as unknown as Record<string, unknown>;
  const nid = node.id;
  const err = (code: string, message: string, path?: string) =>
    errors.push({ code, message, severity: "error", nodeId: nid, path });

  const edge = (name: string, value: unknown) => {
    if (!isNonEmptyString(value)) {
      err("node.missing_edge", `Connect this node's "${name}" output.`, `node[${nid}].${name}`);
    } else if (!ids.has(value)) {
      err("node.unknown_target", `"${name}" points at a node that doesn't exist.`, `node[${nid}].${name}`);
    }
  };

  const predicate = (value: unknown, name: string) => {
    for (const p of predicateErrors(value, `node[${nid}].${name}`)) {
      err("predicate.invalid", p.message, p.path);
    }
  };

  switch (node.type) {
    case "trigger": {
      if (!isNonEmptyString(rec.trigger) || !TRIGGERS.has(rec.trigger)) {
        err("trigger.unknown", "Choose a trigger event.", `node[${nid}].trigger`);
      }
      if (rec.filter !== undefined && rec.filter !== null) predicate(rec.filter, "filter");
      edge("next", rec.next);
      break;
    }
    case "condition": {
      if (!("predicate" in rec)) {
        err("condition.missing_predicate", "Add a condition.", `node[${nid}].predicate`);
      } else {
        predicate(rec.predicate, "predicate");
      }
      edge("true", rec.true);
      edge("false", rec.false);
      break;
    }
    case "action": {
      const action = rec.action;
      if (!isNonEmptyString(action) || !ACTIONS.has(action)) {
        err("action.unknown", "Choose an action.", `node[${nid}].action`);
      } else {
        validateActionParams(action, isRecord(rec.params) ? rec.params : {}, nid, errors);
      }
      edge("next", rec.next);
      break;
    }
    case "bot_step": {
      validateBot(node, ids, errors);
      break;
    }
    case "wait": {
      validateDurationParams(isRecord(rec.params) ? rec.params : {}, nid, "wait", errors);
      edge("next", rec.next);
      break;
    }
    case "end":
      break;
  }
}

function validateActionParams(
  action: string,
  params: Record<string, unknown>,
  nid: string,
  errors: GraphError[],
): void {
  const path = `node[${nid}].params`;
  const err = (message: string) =>
    errors.push({ code: "action.params", message, severity: "error", nodeId: nid, path });

  switch (action) {
    case "assign":
      if (!isNonEmptyString(params.assignee_id) && !isNonEmptyString(params.team_id)) {
        err("Choose a teammate and/or a team to assign to.");
      }
      break;
    case "route_to_team":
      if (!isNonEmptyString(params.team_id)) err("Choose a team to route to.");
      break;
    case "add_tag":
      if (!isNonEmptyString(params.name)) err("Enter a tag name.");
      break;
    case "set_attribute":
      if (!isNonEmptyString(params.target) || !TARGETS.has(params.target)) {
        err("Choose whether to set a conversation or contact attribute.");
      }
      if (!isNonEmptyString(params.key)) err("Enter the attribute key.");
      if (!("value" in params)) err("Enter the value to set.");
      break;
    case "snooze":
      validateDurationParams(params, nid, "snooze", errors);
      break;
    case "send_reply":
      if (!isNonEmptyString(params.body)) err("Enter the reply message.");
      break;
    case "call_webhook": {
      if (!isNonEmptyString(params.url)) err("Enter the webhook URL.");
      // graph.py: only POST is supported in P1.5 (other verbs land with the app framework, P2.9).
      const method = params.method === undefined ? "POST" : params.method;
      if (method !== "POST") err("Webhooks only support POST in this release.");
      const headers = params.headers;
      if (headers !== undefined && headers !== null) {
        const ok =
          typeof headers === "object" &&
          !Array.isArray(headers) &&
          Object.entries(headers as Record<string, unknown>).every(
            ([k, v]) => typeof k === "string" && typeof v === "string",
          );
        if (!ok) err("Webhook headers must be a string→string object.");
      }
      break;
    }
    // close / hand_to_aide: no required params. apply_sla: free (flag-gated).
    default:
      break;
  }
}

function validateDurationParams(
  params: Record<string, unknown>,
  nid: string,
  label: string,
  errors: GraphError[],
): void {
  const path = `node[${nid}].params`;
  const err = (message: string) =>
    errors.push({ code: `${label}.params`, message, severity: "error", nodeId: nid, path });
  const { seconds, until } = params;
  if (seconds !== undefined && seconds !== null) {
    if (typeof seconds !== "number" || !Number.isInteger(seconds) || seconds <= 0) {
      err("Duration must be a positive whole number of seconds.");
    }
  } else if (until !== undefined && until !== null) {
    if (!isNonEmptyString(until)) err("Enter an ISO-8601 date/time to wait until.");
  } else {
    err("Set a duration (seconds) or an 'until' date/time.");
  }
}

function validateBot(node: WorkflowNode, ids: Set<string>, errors: GraphError[]): void {
  const rec = node as unknown as Record<string, unknown>;
  const nid = node.id;
  const path = `node[${nid}].params`;
  const err = (code: string, message: string) =>
    errors.push({ code, message, severity: "error", nodeId: nid, path });

  const bot = rec.bot;
  if (!isNonEmptyString(bot) || !BOTS.has(bot)) {
    err("bot.unknown", "Choose a bot step type.");
    return;
  }
  const params = isRecord(rec.params) ? rec.params : {};
  if (!isNonEmptyString(params.prompt)) err("bot.prompt", "Enter the message to show the customer.");

  const target = (value: unknown, name: string) => {
    if (!isNonEmptyString(value)) {
      err("bot.missing_edge", `Connect the "${name}" branch.`);
    } else if (!ids.has(value)) {
      err("bot.unknown_target", `"${name}" points at a node that doesn't exist.`);
    }
  };

  if (bot === "ask_buttons" || bot === "disambiguate") {
    const options = params.options;
    if (!Array.isArray(options) || options.length < 1) {
      err("bot.options", "Add at least one option.");
    } else {
      const seen = new Set<string>();
      for (const opt of options) {
        if (!isRecord(opt)) {
          err("bot.option_shape", "Each option must be an object.");
          continue;
        }
        if (!isNonEmptyString(opt.label)) err("bot.option_label", "Each option needs a label.");
        if (!isNonEmptyString(opt.value)) {
          err("bot.option_value", "Each option needs a value.");
        } else if (seen.has(opt.value)) {
          err("bot.option_dup", `Duplicate option value "${opt.value}".`);
        } else {
          seen.add(opt.value);
        }
        target(opt.next, "option");
      }
    }
    if (params.default_next !== undefined && params.default_next !== null) {
      target(params.default_next, "default");
    }
  } else {
    // collect
    if (!isNonEmptyString(params.target) || !TARGETS.has(params.target)) {
      err("bot.target", "Choose where to store the reply (conversation or contact).");
    }
    if (!isNonEmptyString(params.key)) err("bot.key", "Enter the attribute key to store the reply.");
    target(params.next, "next");
  }
}
