/**
 * Pure graph traversal helpers shared by the validator, the canvas mappers, and the e2e mock's
 * mini-executor. Mirrors the edge model in `automation/graph.py` (`outgoing`, `_bot_targets`).
 *
 * Edges in the persisted graph are **fields on nodes**, not separate objects:
 *   trigger / action / wait → `next`
 *   condition               → `true` / `false`
 *   bot_step                → `params.options[].next`, `params.default_next`, or `collect`'s `next`
 *
 * On the React Flow canvas each such edge slot is a named **source handle** on the node; every
 * non-trigger node has a single target handle `"in"`.
 */

import type { WorkflowGraph, WorkflowNode, TriggerNode } from "./contract";

export const TARGET_HANDLE = "in";

/** A directed edge slot: which source handle on a node points at which target node id. */
export interface EdgeSlot {
  /** React Flow source handle id ("next" | "true" | "false" | `opt:<id|value>` | "default"). */
  handle: string;
  /** The target node id, or null when the slot is not yet wired (draft in progress). */
  target: string | null;
  /** Optional human label for the rendered edge (bot options). */
  label?: string;
}

/** Stable source-handle id for a bot option: its `id` if present (UI-created), else its `value`
 * (externally-authored). Keeps canvas wiring stable across value edits. */
export function optionHandleId(opt: { id?: unknown; value?: unknown }): string | null {
  const key = str(opt.id) ?? str(opt.value);
  return key === null ? null : `opt:${key}`;
}

function asRecord(node: WorkflowNode): Record<string, unknown> {
  return node as unknown as Record<string, unknown>;
}

function str(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

/** The single trigger node (there is exactly one in a valid graph), or undefined. */
export function triggerOf(graph: WorkflowGraph): TriggerNode | undefined {
  return graph.nodes.find((n): n is TriggerNode => n.type === "trigger");
}

export function nodesById(graph: WorkflowGraph): Map<string, WorkflowNode> {
  const map = new Map<string, WorkflowNode>();
  for (const node of graph.nodes) map.set(node.id, node);
  return map;
}

/** Bot-step transition targets (mirror of `graph.py._bot_targets`). */
export function botTargets(params: Record<string, unknown> | undefined): string[] {
  const out: string[] = [];
  const options = params?.options;
  if (Array.isArray(options)) {
    for (const opt of options) {
      if (opt && typeof opt === "object") {
        const t = str((opt as Record<string, unknown>).next);
        if (t) out.push(t);
      }
    }
  }
  for (const key of ["next", "default_next"] as const) {
    const t = str(params?.[key]);
    if (t) out.push(t);
  }
  return out;
}

/** The node ids a node can transition to (mirror of `graph.py.outgoing`). Used for reachability. */
export function outgoing(node: WorkflowNode): string[] {
  const rec = asRecord(node);
  switch (node.type) {
    case "trigger":
    case "action":
    case "wait": {
      const t = str(rec.next);
      return t ? [t] : [];
    }
    case "condition": {
      return [str(rec.true), str(rec.false)].filter((t): t is string => t !== null);
    }
    case "bot_step":
      return botTargets(rec.params as Record<string, unknown> | undefined);
    case "end":
      return [];
    default:
      return []; // unknown/malformed node type (defensive; never in a valid graph)
  }
}

/** Named edge slots for a node (for rendering React Flow edges from the domain graph). */
export function edgeSlots(node: WorkflowNode): EdgeSlot[] {
  const rec = asRecord(node);
  switch (node.type) {
    case "trigger":
    case "action":
    case "wait":
      return [{ handle: "next", target: str(rec.next) }];
    case "condition":
      return [
        { handle: "true", target: str(rec.true) },
        { handle: "false", target: str(rec.false) },
      ];
    case "bot_step": {
      const params = (rec.params ?? {}) as Record<string, unknown>;
      if (node.bot === "collect") return [{ handle: "next", target: str(params.next) }];
      const slots: EdgeSlot[] = [];
      const options = Array.isArray(params.options) ? params.options : [];
      for (const opt of options) {
        if (opt && typeof opt === "object") {
          const o = opt as Record<string, unknown>;
          const handle = optionHandleId(o);
          if (handle !== null) {
            slots.push({
              handle,
              target: str(o.next),
              label: str(o.label) ?? str(o.value) ?? undefined,
            });
          }
        }
      }
      if (params.default_next !== undefined) {
        slots.push({ handle: "default", target: str(params.default_next) });
      }
      return slots;
    }
    case "end":
      return [];
    default:
      return []; // unknown/malformed node type (defensive)
  }
}

/** Reachable node ids from the trigger, following `outgoing` (mirror of graph.py reachability). */
export function reachableFrom(graph: WorkflowGraph): Set<string> {
  const trigger = triggerOf(graph);
  const reachable = new Set<string>();
  if (!trigger) return reachable;
  const byId = nodesById(graph);
  const stack: string[] = [trigger.id];
  while (stack.length > 0) {
    const cur = stack.pop() as string;
    if (reachable.has(cur)) continue;
    reachable.add(cur);
    const node = byId.get(cur);
    if (node) stack.push(...outgoing(node));
  }
  return reachable;
}

/** True if the graph contains a back-edge (a node reachable from itself). Cycles are rejected at
 * publish (graph.py `_require_acyclic`: a back-edge could strand a run), so callers surface this as
 * a blocking error. */
export function hasCycle(graph: WorkflowGraph): boolean {
  const byId = nodesById(graph);
  const state = new Map<string, 0 | 1 | 2>(); // 0=unseen 1=on-stack 2=done
  const trigger = triggerOf(graph);
  if (!trigger) return false;

  const visit = (id: string): boolean => {
    const s = state.get(id) ?? 0;
    if (s === 1) return true;
    if (s === 2) return false;
    state.set(id, 1);
    const node = byId.get(id);
    if (node) {
      for (const next of outgoing(node)) {
        if (byId.has(next) && visit(next)) return true;
      }
    }
    state.set(id, 2);
    return false;
  };
  return visit(trigger.id);
}
