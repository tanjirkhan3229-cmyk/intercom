import type { ActionType, Predicate, WorkflowNode } from "@/lib/workflows/contract";
import { optionHandleId } from "@/lib/workflows/graph-utils";

/**
 * Palette definitions, the new-node factory, per-node display metadata, and the source-handle model
 * (which must match the mapper's handle ids in `graph-utils.ts` / `mappers.ts`).
 */

export type PaletteKind =
  | "trigger"
  | "condition"
  | "collect"
  | "ask"
  | "action"
  | "wait"
  | "end";

export interface PaletteItem {
  kind: PaletteKind;
  label: string;
  description: string;
}

export const PALETTE: readonly PaletteItem[] = [
  { kind: "trigger", label: "Trigger", description: "When this happens…" },
  { kind: "condition", label: "Condition", description: "Branch on attributes" },
  { kind: "collect", label: "Collect info", description: "Ask & store a reply" },
  { kind: "ask", label: "Ask (buttons)", description: "Offer choices" },
  { kind: "action", label: "Action", description: "Do something" },
  { kind: "wait", label: "Wait", description: "Pause for a duration" },
  { kind: "end", label: "End", description: "Finish the run" },
];

export function newNodeId(): string {
  const rand = globalThis.crypto?.randomUUID?.().slice(0, 8) ?? `${Math.floor(Math.random() * 1e9)}`;
  return `n_${rand}`;
}

const EMPTY_PREDICATE: Predicate = { op: "and", clauses: [] };

/** Build a fresh node of the given palette kind (edges/params default to unset). */
export function createNode(kind: PaletteKind, id: string): WorkflowNode {
  switch (kind) {
    case "trigger":
      return { id, type: "trigger", trigger: "conversation.created", next: "" };
    case "condition":
      return { id, type: "condition", predicate: EMPTY_PREDICATE, true: "", false: "" };
    case "collect":
      return {
        id,
        type: "bot_step",
        bot: "collect",
        params: { prompt: "", target: "contact", key: "", next: "" },
      };
    case "ask":
      return {
        id,
        type: "bot_step",
        bot: "ask_buttons",
        params: { prompt: "", options: [{ id: newNodeId(), label: "Yes", value: "yes", next: "" }] },
      };
    case "action":
      return { id, type: "action", action: "send_reply", params: { body: "" }, next: "" };
    case "wait":
      return { id, type: "wait", params: { seconds: 3600 }, next: "" };
    case "end":
      return { id, type: "end" };
  }
}

export interface HandleDef {
  id: string;
  label: string;
}

/** Source handles for a node — must line up with `graph-utils.edgeSlots` handle ids. */
export function sourceHandles(node: WorkflowNode): HandleDef[] {
  switch (node.type) {
    case "trigger":
    case "action":
    case "wait":
      return [{ id: "next", label: "" }];
    case "condition":
      return [
        { id: "true", label: "yes" },
        { id: "false", label: "no" },
      ];
    case "bot_step": {
      if (node.bot === "collect") return [{ id: "next", label: "" }];
      const opts = Array.isArray(node.params.options) ? node.params.options : [];
      const handles = opts
        .map((o) => ({ handle: optionHandleId(o), label: o.label || o.value }))
        .filter((h): h is { handle: string; label: string } => h.handle !== null)
        .map((h) => ({ id: h.handle, label: h.label }));
      return [...handles, { id: "default", label: "default" }];
    }
    case "end":
      return [];
  }
}

export function hasTargetHandle(node: WorkflowNode): boolean {
  return node.type !== "trigger";
}

export interface NodeMeta {
  title: string;
  subtitle: string;
}

function describePredicate(p: Predicate | undefined): string {
  if (!p) return "no condition";
  if ("clauses" in p) {
    const n = p.clauses.length;
    if (n === 0) return "always";
    return `${p.op === "and" ? "all" : "any"} of ${n}`;
  }
  if ("clause" in p) return `not (${describePredicate(p.clause)})`;
  if (p.op === "exists") return `${p.field} is set`;
  if (p.op === "not_exists") return `${p.field} is not set`;
  const val = "value" in p ? JSON.stringify(p.value) : "";
  return `${p.field} ${p.op} ${val}`;
}

/** Human labels for action types — the single source, reused by the inspector's action dropdown. */
export const ACTION_LABELS: Record<ActionType, string> = {
  assign: "Assign",
  route_to_team: "Route to team",
  add_tag: "Add tag",
  set_attribute: "Set attribute",
  snooze: "Snooze",
  close: "Close conversation",
  send_reply: "Send reply",
  hand_to_aide: "Hand to Aide",
  call_webhook: "Call webhook",
  apply_sla: "Apply SLA",
};

export function nodeMeta(node: WorkflowNode): NodeMeta {
  switch (node.type) {
    case "trigger":
      return { title: "Trigger", subtitle: node.trigger };
    case "condition":
      return { title: "Condition", subtitle: describePredicate(node.predicate) };
    case "action":
      return {
        title: ACTION_LABELS[node.action] ?? "Action",
        subtitle:
          node.action === "route_to_team" ? `team: ${node.params.team_id || "—"}` : node.action,
      };
    case "bot_step":
      return {
        title:
          node.bot === "collect" ? "Collect info" : node.bot === "ask_buttons" ? "Ask" : "Disambiguate",
        subtitle:
          node.bot === "collect"
            ? `→ ${node.params.target}.${node.params.key || "—"}`
            : `${node.params.prompt?.slice(0, 30) || "…"}`,
      };
    case "wait":
      return {
        title: "Wait",
        subtitle: "seconds" in node.params ? `${node.params.seconds}s` : `until ${node.params.until}`,
      };
    case "end":
      return { title: "End", subtitle: "" };
  }
}
