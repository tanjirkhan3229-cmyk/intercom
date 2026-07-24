import { describe, expect, it } from "vitest";
import type { WorkflowGraph } from "./contract";
import { flowToGraph, graphToFlow } from "./mappers";
import { validateGraph } from "./validate";
import { acceptanceGraph } from "./__fixtures__/sample-graph";

describe("graphToFlow", () => {
  it("creates one React Flow node per graph node, honoring ui positions", () => {
    const { nodes } = graphToFlow(acceptanceGraph());
    expect(nodes).toHaveLength(7);
    const trigger = nodes.find((n) => n.id === "t");
    expect(trigger?.type).toBe("trigger");
    expect(trigger?.position).toEqual({ x: 80, y: 80 });
  });

  it("projects edge slots into typed edges with handles + branch labels", () => {
    const { edges } = graphToFlow(acceptanceGraph());
    // trigger.next
    expect(edges.find((e) => e.source === "t")?.target).toBe("c1");
    // condition true/false with labels
    const cond = edges.filter((e) => e.source === "c1");
    expect(cond.map((e) => e.sourceHandle).sort()).toEqual(["false", "true"]);
    expect(cond.find((e) => e.sourceHandle === "true")?.label).toBe("yes");
    expect(cond.find((e) => e.sourceHandle === "true")?.target).toBe("b1");
    expect(cond.find((e) => e.sourceHandle === "false")?.target).toBe("end");
  });

  it("auto-lays-out nodes lacking a ui hint", () => {
    const g: WorkflowGraph = {
      nodes: [
        { id: "t", type: "trigger", trigger: "conversation.created", next: "end" },
        { id: "end", type: "end" },
      ],
    };
    const { nodes } = graphToFlow(g);
    const positions = nodes.map((n) => n.position);
    // distinct, non-overlapping columns
    expect(positions[0]).not.toEqual(positions[1]);
    expect(positions.every((p) => Number.isFinite(p.x) && Number.isFinite(p.y))).toBe(true);
  });
});

describe("flowToGraph round-trip", () => {
  it("is stable: graph → flow → graph preserves structure", () => {
    const original = acceptanceGraph();
    const { nodes, edges } = graphToFlow(original);
    const back = flowToGraph(nodes, edges);
    // Compare as maps keyed by id (order-independent).
    const byId = (g: WorkflowGraph) => Object.fromEntries(g.nodes.map((n) => [n.id, n]));
    expect(byId(back)).toEqual(byId(original));
  });

  it("keeps the round-tripped graph valid", () => {
    const { nodes, edges } = graphToFlow(acceptanceGraph());
    const back = flowToGraph(nodes, edges);
    expect(validateGraph(back).filter((e) => e.severity === "error")).toEqual([]);
  });

  it("writes moved canvas positions back into ui", () => {
    const { nodes, edges } = graphToFlow(acceptanceGraph());
    const moved = nodes.map((n) => (n.id === "t" ? { ...n, position: { x: 12, y: 34 } } : n));
    const back = flowToGraph(moved, edges);
    const trigger = back.nodes.find((n) => n.id === "t");
    expect(trigger?.ui).toEqual({ x: 12, y: 34 });
  });

  it("folds a rewired edge back into the correct node field", () => {
    const { nodes, edges } = graphToFlow(acceptanceGraph());
    // Rewire condition c1's `false` branch from end → b1.
    const rewired = edges.map((e) =>
      e.source === "c1" && e.sourceHandle === "false" ? { ...e, target: "b1" } : e,
    );
    const back = flowToGraph(nodes, rewired);
    const c1 = back.nodes.find((n) => n.id === "c1");
    expect((c1 as { false: string }).false).toBe("b1");
    expect((c1 as { true: string }).true).toBe("b1");
  });

  it("round-trips bot ask_buttons option targets", () => {
    const g: WorkflowGraph = {
      nodes: [
        { id: "t", type: "trigger", trigger: "conversation.created", next: "b" },
        {
          id: "b",
          type: "bot_step",
          bot: "ask_buttons",
          params: {
            prompt: "Which?",
            options: [
              { id: "s1", label: "Sales", value: "sales", next: "end" },
              { id: "s2", label: "Support", value: "support", next: "end" },
            ],
            default_next: "end",
          },
        },
        { id: "end", type: "end" },
      ],
    };
    const { nodes, edges } = graphToFlow(g);
    // two option edges + one default edge from the bot node
    expect(edges.filter((e) => e.source === "b")).toHaveLength(3);
    const back = flowToGraph(nodes, edges);
    // Round-trip adds `ui` (persisted positions); compare modulo layout since `g` has none.
    const stripUi = (n: WorkflowGraph["nodes"][number]) => {
      const copy = { ...n };
      delete copy.ui;
      return copy;
    };
    const byId = (x: WorkflowGraph) => Object.fromEntries(x.nodes.map((n) => [n.id, stripUi(n)]));
    expect(byId(back)).toEqual(byId(g));
  });

  it("backfills stable ids onto id-less bot options at load (external graphs)", () => {
    const g: WorkflowGraph = {
      nodes: [
        { id: "t", type: "trigger", trigger: "conversation.created", next: "b" },
        {
          id: "b",
          type: "bot_step",
          bot: "ask_buttons",
          params: { prompt: "p", options: [{ label: "A", value: "a", next: "end" }] },
        },
        { id: "end", type: "end" },
      ],
    };
    // No id on the option → graphToFlow backfills one, and the edge handle keys on it (not "opt:a").
    const { edges } = graphToFlow(g);
    expect(edges.find((e) => e.source === "b")?.sourceHandle).toMatch(/^opt:o_/);
  });

  it("keys bot option edges on a stable id, so renaming an option value keeps the wiring", () => {
    const g: WorkflowGraph = {
      nodes: [
        { id: "t", type: "trigger", trigger: "conversation.created", next: "b" },
        {
          id: "b",
          type: "bot_step",
          bot: "ask_buttons",
          params: { prompt: "Pick", options: [{ id: "o1", label: "Sales", value: "sales", next: "end" }] },
        },
        { id: "end", type: "end" },
      ],
    };
    const { nodes, edges } = graphToFlow(g);
    // Handle is keyed on the option id, not its (editable) value.
    expect(edges.find((e) => e.source === "b")?.sourceHandle).toBe("opt:o1");

    // Rename the option's value on the canvas node; the edge (still keyed by opt:o1) is preserved.
    const renamed = nodes.map((n) => {
      if (n.id !== "b") return n;
      const node = JSON.parse(JSON.stringify(n.data.node)) as Record<string, unknown>;
      (node.params as { options: Array<Record<string, unknown>> }).options[0]!.value = "leads";
      return { ...n, data: { node: node as unknown as WorkflowGraph["nodes"][number] } };
    });
    const back = flowToGraph(renamed, edges);
    const b = back.nodes.find((n) => n.id === "b") as unknown as {
      params: { options: Array<{ value: string; next: string }> };
    };
    expect(b.params.options[0]!.value).toBe("leads");
    expect(b.params.options[0]!.next).toBe("end"); // wiring intact despite the value change
  });

  it("represents an unwired output as an empty target (caught by validation)", () => {
    const { nodes, edges } = graphToFlow(acceptanceGraph());
    const dropped = edges.filter((e) => !(e.source === "a2" && e.sourceHandle === "next"));
    const back = flowToGraph(nodes, dropped);
    const a2 = back.nodes.find((n) => n.id === "a2");
    expect((a2 as { next: string }).next).toBe("");
    expect(validateGraph(back).some((e) => e.code === "node.missing_edge")).toBe(true);
  });
});
