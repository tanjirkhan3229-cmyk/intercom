/**
 * Bidirectional mapping between the persisted workflow `graph` JSON and the React Flow
 * `{nodes, edges}` the canvas renders. Pure and unit-tested for round-trip stability — this is the
 * load-bearing seam between the domain model and the editor.
 *
 * - `graphToFlow` reads each node's `ui:{x,y}` layout hint (auto-lays-out nodes that lack one), and
 *   projects the node's edge slots (`next`/`true`/`false`/bot targets) into React Flow edges.
 * - `flowToGraph` writes canvas positions back into `ui`, and folds React Flow edges back into the
 *   correct node fields by source-handle id — preserving all other config (predicates, params,
 *   option labels) verbatim.
 */

import type { Edge, Node } from "@xyflow/react";
import type { WorkflowGraph, WorkflowNode } from "./contract";
import {
  edgeSlots,
  nodesById,
  optionHandleId,
  outgoing,
  TARGET_HANDLE,
  triggerOf,
} from "./graph-utils";

export type WorkflowNodeData = { node: WorkflowNode };
export type WFNode = Node<WorkflowNodeData>;
export type WFEdge = Edge;

const COL_WIDTH = 300;
const ROW_HEIGHT = 140;
const ORIGIN_X = 80;
const ORIGIN_Y = 80;

/** Deep clone a JSON-serializable node (graph nodes are always JSON). */
function cloneNode(node: WorkflowNode): WorkflowNode {
  return JSON.parse(JSON.stringify(node)) as WorkflowNode;
}

function genOptionId(): string {
  return `o_${globalThis.crypto?.randomUUID?.().slice(0, 8) ?? Math.random().toString(36).slice(2, 10)}`;
}

/** Backfill stable ids onto bot options that lack one (e.g. a graph authored outside this UI, keyed
 * only by `value`). Called once at load so canvas wiring keys on the id from the start — editing an
 * option's value can then never sever its connection. Returns the same graph if nothing changed. */
export function ensureOptionIds(graph: WorkflowGraph): WorkflowGraph {
  let changed = false;
  const nodes = graph.nodes.map((node) => {
    if (node.type !== "bot_step") return node;
    const params = (node as unknown as { params?: Record<string, unknown> }).params;
    const options = params?.options;
    if (!Array.isArray(options)) return node;
    let optChanged = false;
    const newOptions = options.map((o) => {
      if (o && typeof o === "object" && typeof (o as Record<string, unknown>).id !== "string") {
        optChanged = true;
        return { ...(o as Record<string, unknown>), id: genOptionId() };
      }
      return o;
    });
    if (!optChanged) return node;
    changed = true;
    return { ...node, params: { ...params, options: newOptions } } as WorkflowNode;
  });
  return changed ? { nodes } : graph;
}

/** BFS layered layout: column = distance from trigger, row = order seen at that depth. Used only
 * to position nodes that don't carry a `ui` hint, so a freshly-authored graph looks sensible. */
function autoLayout(graph: WorkflowGraph): Map<string, { x: number; y: number }> {
  const positions = new Map<string, { x: number; y: number }>();
  const byId = nodesById(graph);
  const depth = new Map<string, number>();
  const trigger = triggerOf(graph);

  const order: string[] = [];
  if (trigger) {
    const queue: Array<{ id: string; d: number }> = [{ id: trigger.id, d: 0 }];
    const seen = new Set<string>();
    while (queue.length > 0) {
      const { id, d } = queue.shift() as { id: string; d: number };
      if (seen.has(id)) continue;
      seen.add(id);
      depth.set(id, d);
      order.push(id);
      const node = byId.get(id);
      if (node) for (const next of outgoing(node)) if (byId.has(next)) queue.push({ id: next, d: d + 1 });
    }
  }
  // Any node not reached from the trigger still needs a slot (orphans while editing).
  let maxDepth = 0;
  for (const d of depth.values()) maxDepth = Math.max(maxDepth, d);
  for (const node of graph.nodes) {
    if (!depth.has(node.id)) {
      maxDepth += 1;
      depth.set(node.id, maxDepth);
      order.push(node.id);
    }
  }

  const rowByCol = new Map<number, number>();
  for (const id of order) {
    const col = depth.get(id) ?? 0;
    const row = rowByCol.get(col) ?? 0;
    rowByCol.set(col, row + 1);
    positions.set(id, { x: ORIGIN_X + col * COL_WIDTH, y: ORIGIN_Y + row * ROW_HEIGHT });
  }
  return positions;
}

export function edgeLabel(handle: string): string | undefined {
  if (handle === "true") return "yes";
  if (handle === "false") return "no";
  if (handle === "default") return "default";
  // `opt:` handles key on a stable id, not the option value, so we can't derive a nice label from
  // the handle alone — graphToFlow supplies it from the option (EdgeSlot.label).
  return undefined;
}

export function graphToFlow(input: WorkflowGraph): { nodes: WFNode[]; edges: WFEdge[] } {
  const graph = ensureOptionIds(input); // stabilize bot-option handle keys before wiring edges
  const layout = autoLayout(graph);
  const nodes: WFNode[] = graph.nodes.map((node) => {
    const pos = node.ui ?? layout.get(node.id) ?? { x: ORIGIN_X, y: ORIGIN_Y };
    return {
      id: node.id,
      type: node.type,
      position: { x: pos.x, y: pos.y },
      data: { node },
    };
  });

  const edges: WFEdge[] = [];
  for (const node of graph.nodes) {
    for (const slot of edgeSlots(node)) {
      if (slot.target === null) continue;
      edges.push({
        id: `e:${node.id}:${slot.handle}`,
        source: node.id,
        sourceHandle: slot.handle,
        target: slot.target,
        targetHandle: TARGET_HANDLE,
        label: slot.label ?? edgeLabel(slot.handle),
      });
    }
  }
  return { nodes, edges };
}

/** Index edges as source -> handle -> target (last write wins; connect-time keeps it 1:1). */
function targetsBySource(edges: WFEdge[]): Map<string, Map<string, string>> {
  const map = new Map<string, Map<string, string>>();
  for (const e of edges) {
    const handle = e.sourceHandle ?? "next";
    let inner = map.get(e.source);
    if (!inner) {
      inner = new Map<string, string>();
      map.set(e.source, inner);
    }
    inner.set(handle, e.target);
  }
  return map;
}

export function flowToGraph(nodes: WFNode[], edges: WFEdge[]): WorkflowGraph {
  const bySource = targetsBySource(edges);

  const out: WorkflowNode[] = nodes.map((rf) => {
    const node = cloneNode(rf.data.node);
    node.ui = { x: Math.round(rf.position.x), y: Math.round(rf.position.y) };
    const handles = bySource.get(rf.id) ?? new Map<string, string>();
    const rec = node as unknown as Record<string, unknown>;

    switch (node.type) {
      case "trigger":
      case "action":
      case "wait":
        rec.next = handles.get("next") ?? "";
        break;
      case "condition":
        rec.true = handles.get("true") ?? "";
        rec.false = handles.get("false") ?? "";
        break;
      case "bot_step": {
        const params = (rec.params ?? {}) as Record<string, unknown>;
        if (node.bot === "collect") {
          params.next = handles.get("next") ?? "";
        } else {
          const options = Array.isArray(params.options) ? params.options : [];
          for (const opt of options) {
            if (opt && typeof opt === "object") {
              const o = opt as Record<string, unknown>;
              const handle = optionHandleId(o);
              o.next = (handle && handles.get(handle)) ?? "";
            }
          }
          if ("default_next" in params || handles.has("default")) {
            const d = handles.get("default");
            if (d !== undefined) params.default_next = d;
            else delete params.default_next;
          }
        }
        rec.params = params;
        break;
      }
      case "end":
        break;
    }
    return node;
  });

  return { nodes: out };
}
