import { describe, expect, it } from "vitest";
import type { WorkflowGraph } from "./contract";
import { validateGraph } from "./validate";
import { acceptanceGraph } from "./__fixtures__/sample-graph";

const codes = (g: unknown) => validateGraph(g).map((e) => e.code);

describe("validateGraph (mirror of graph.py.validate_graph)", () => {
  it("accepts the acceptance-scenario graph with no errors", () => {
    const errs = validateGraph(acceptanceGraph());
    expect(errs.filter((e) => e.severity === "error")).toEqual([]);
  });

  it("rejects a non-graph or empty node list", () => {
    expect(codes(null)).toContain("graph.shape");
    expect(codes({ nodes: [] })).toContain("graph.empty");
  });

  it("requires exactly one trigger", () => {
    expect(codes({ nodes: [{ id: "a", type: "end" }] })).toContain("graph.trigger_count");
    expect(
      codes({
        nodes: [
          { id: "t1", type: "trigger", trigger: "conversation.created", next: "e" },
          { id: "t2", type: "trigger", trigger: "contact.created", next: "e" },
          { id: "e", type: "end" },
        ],
      }),
    ).toContain("graph.trigger_count");
  });

  it("flags duplicate ids and unknown types", () => {
    const g = {
      nodes: [
        { id: "t", type: "trigger", trigger: "conversation.created", next: "t" },
        { id: "t", type: "frobnicate" },
      ],
    };
    const c = codes(g);
    expect(c).toContain("node.duplicate_id");
    expect(c).toContain("node.unknown_type");
  });

  it("flags a missing edge and an unknown target", () => {
    const missing: WorkflowGraph = {
      nodes: [
        { id: "t", type: "trigger", trigger: "conversation.created", next: "" } as never,
        { id: "e", type: "end" },
      ],
    };
    expect(codes(missing)).toContain("node.missing_edge");

    const dangling: WorkflowGraph = {
      nodes: [{ id: "t", type: "trigger", trigger: "conversation.created", next: "ghost" }],
    };
    expect(codes(dangling)).toContain("node.unknown_target");
  });

  it("flags unreachable (orphan) nodes", () => {
    const g: WorkflowGraph = {
      nodes: [
        { id: "t", type: "trigger", trigger: "conversation.created", next: "end" },
        { id: "end", type: "end" },
        { id: "orphan", type: "end" },
      ],
    };
    const errs = validateGraph(g);
    expect(errs.some((e) => e.code === "node.unreachable" && e.nodeId === "orphan")).toBe(true);
  });

  it("enforces action param requirements", () => {
    const g: WorkflowGraph = {
      nodes: [
        { id: "t", type: "trigger", trigger: "conversation.created", next: "a" },
        { id: "a", type: "action", action: "route_to_team", params: {} as never, next: "end" },
        { id: "end", type: "end" },
      ],
    };
    expect(codes(g)).toContain("action.params");
  });

  it("enforces collect bot-step requirements", () => {
    const g: WorkflowGraph = {
      nodes: [
        { id: "t", type: "trigger", trigger: "conversation.created", next: "b" },
        { id: "b", type: "bot_step", bot: "collect", params: { prompt: "hi" } as never },
        { id: "end", type: "end" },
      ],
    };
    const c = codes(g);
    expect(c).toContain("bot.key");
    expect(c).toContain("bot.missing_edge"); // collect's `next` is unset
  });

  it("enforces ask_buttons options (presence + unique values)", () => {
    const g: WorkflowGraph = {
      nodes: [
        { id: "t", type: "trigger", trigger: "conversation.created", next: "b" },
        {
          id: "b",
          type: "bot_step",
          bot: "ask_buttons",
          params: {
            prompt: "Pick",
            options: [
              { label: "A", value: "x", next: "end" },
              { label: "B", value: "x", next: "end" },
            ],
          },
        },
        { id: "end", type: "end" },
      ],
    };
    expect(codes(g)).toContain("bot.option_dup");
  });

  it("requires a valid duration for wait", () => {
    const g: WorkflowGraph = {
      nodes: [
        { id: "t", type: "trigger", trigger: "conversation.created", next: "w" },
        { id: "w", type: "wait", params: { seconds: 0 } as never, next: "end" },
        { id: "end", type: "end" },
      ],
    };
    expect(codes(g)).toContain("wait.params");
  });

  it("rejects a back-edge (loops unsupported, mirrors _require_acyclic)", () => {
    const g: WorkflowGraph = {
      nodes: [
        { id: "t", type: "trigger", trigger: "conversation.created", next: "c" },
        {
          id: "c",
          type: "condition",
          predicate: { op: "exists", field: "contact.email" },
          true: "t",
          false: "end",
        },
        { id: "end", type: "end" },
      ],
    };
    const errs = validateGraph(g);
    expect(errs.some((e) => e.code === "graph.cycle" && e.severity === "error")).toBe(true);
  });

  it("restricts call_webhook to POST with a string→string headers map", () => {
    const base = (params: Record<string, unknown>): WorkflowGraph => ({
      nodes: [
        { id: "t", type: "trigger", trigger: "conversation.created", next: "a" },
        { id: "a", type: "action", action: "call_webhook", params: params as never, next: "end" },
        { id: "end", type: "end" },
      ],
    });
    expect(codes(base({ url: "https://x.test", method: "GET" }))).toContain("action.params");
    expect(codes(base({ url: "https://x.test", headers: { a: 1 } }))).toContain("action.params");
    expect(
      validateGraph(base({ url: "https://x.test", method: "POST", headers: { a: "b" } })).filter(
        (e) => e.severity === "error",
      ),
    ).toEqual([]);
  });

  it("surfaces an invalid condition predicate", () => {
    const g: WorkflowGraph = {
      nodes: [
        { id: "t", type: "trigger", trigger: "conversation.created", next: "c" },
        { id: "c", type: "condition", predicate: { op: "bogus" } as never, true: "end", false: "end" },
        { id: "end", type: "end" },
      ],
    };
    expect(codes(g)).toContain("predicate.invalid");
  });
});
