import { describe, expect, it } from "vitest";
import { evaluate, isValidPredicate, opsForDataType, predicateErrors } from "./predicate";

describe("predicateErrors (mirror of validate_predicate)", () => {
  it("accepts well-formed leaves and groups", () => {
    expect(predicateErrors({ op: "eq", field: "a.b", value: 1 })).toEqual([]);
    expect(predicateErrors({ op: "exists", field: "x" })).toEqual([]);
    expect(predicateErrors({ op: "in", field: "x", value: [1, 2] })).toEqual([]);
    expect(
      predicateErrors({
        op: "and",
        clauses: [
          { op: "eq", field: "a", value: 1 },
          { op: "not", clause: { op: "exists", field: "b" } },
        ],
      }),
    ).toEqual([]);
  });

  it("rejects unknown ops", () => {
    const errs = predicateErrors({ op: "matches", field: "a", value: 1 });
    expect(errs).toHaveLength(1);
    expect(errs[0]?.message).toContain("unknown predicate op");
  });

  it("requires a clauses list for and/or", () => {
    expect(predicateErrors({ op: "and" })[0]?.path).toBe("predicate.clauses");
    expect(predicateErrors({ op: "or", clauses: {} })[0]?.message).toContain("'clauses' list");
  });

  it("requires a clause for not", () => {
    expect(predicateErrors({ op: "not" })[0]?.message).toContain("requires a 'clause'");
  });

  it("requires a non-empty string field for comparisons", () => {
    expect(predicateErrors({ op: "eq", value: 1 })[0]?.message).toContain("'field'");
    expect(predicateErrors({ op: "eq", field: "", value: 1 })[0]?.message).toContain("'field'");
  });

  it("requires a value (except presence ops)", () => {
    expect(predicateErrors({ op: "eq", field: "a" })[0]?.message).toContain("requires a 'value'");
    expect(predicateErrors({ op: "exists", field: "a" })).toEqual([]);
  });

  it("requires a list value for in", () => {
    expect(predicateErrors({ op: "in", field: "a", value: "x" })[0]?.message).toContain("list");
  });

  it("collects every problem across a tree", () => {
    const errs = predicateErrors({
      op: "and",
      clauses: [{ op: "eq", field: "" }, { op: "bogus", field: "b", value: 1 }],
    });
    expect(errs.length).toBe(2);
  });

  it("guards against pathological nesting depth", () => {
    let node: unknown = { op: "exists", field: "a" };
    for (let i = 0; i < 40; i++) node = { op: "not", clause: node };
    expect(predicateErrors(node).some((e) => e.message.includes("too deeply"))).toBe(true);
  });
});

describe("evaluate (mirror of predicates.py.evaluate)", () => {
  const ctx = {
    conversation: { state: "open", priority: true, count: 3 },
    contact: { email: "a@b.com", tags: ["vip", "eu"] },
    env: { within_office_hours: false },
  };

  it("resolves dotted paths and compares equality", () => {
    expect(evaluate({ op: "eq", field: "conversation.state", value: "open" }, ctx)).toBe(true);
    expect(evaluate({ op: "eq", field: "conversation.state", value: "closed" }, ctx)).toBe(false);
    expect(evaluate({ op: "eq", field: "env.within_office_hours", value: false }, ctx)).toBe(true);
  });

  it("treats a missing field as not-equal for ne and false for eq", () => {
    expect(evaluate({ op: "eq", field: "nope.here", value: "x" }, ctx)).toBe(false);
    expect(evaluate({ op: "ne", field: "nope.here", value: "x" }, ctx)).toBe(true);
  });

  it("handles presence ops", () => {
    expect(evaluate({ op: "exists", field: "contact.email" }, ctx)).toBe(true);
    expect(evaluate({ op: "not_exists", field: "contact.phone" }, ctx)).toBe(true);
    expect(evaluate({ op: "exists", field: "contact.phone" }, ctx)).toBe(false);
  });

  it("handles in and contains", () => {
    expect(evaluate({ op: "in", field: "conversation.state", value: ["open", "snoozed"] }, ctx)).toBe(true);
    expect(evaluate({ op: "contains", field: "contact.tags", value: "vip" }, ctx)).toBe(true);
    expect(evaluate({ op: "contains", field: "contact.email", value: "@b." }, ctx)).toBe(true);
    expect(evaluate({ op: "contains", field: "contact.tags", value: "nope" }, ctx)).toBe(false);
  });

  it("orders only like-typed scalars", () => {
    expect(evaluate({ op: "gt", field: "conversation.count", value: 2 }, ctx)).toBe(true);
    expect(evaluate({ op: "lte", field: "conversation.count", value: 3 }, ctx)).toBe(true);
    // mismatched types → false, never throws
    expect(evaluate({ op: "gt", field: "conversation.state", value: 2 }, ctx)).toBe(false);
    expect(evaluate({ op: "gt", field: "missing.field", value: 2 }, ctx)).toBe(false);
  });

  it("combines with and/or/not", () => {
    expect(
      evaluate(
        {
          op: "and",
          clauses: [
            { op: "eq", field: "conversation.state", value: "open" },
            { op: "eq", field: "env.within_office_hours", value: false },
          ],
        },
        ctx,
      ),
    ).toBe(true);
    expect(evaluate({ op: "not", clause: { op: "eq", field: "conversation.state", value: "open" } }, ctx)).toBe(false);
    expect(evaluate({ op: "or", clauses: [] }, ctx)).toBe(false);
    expect(evaluate({ op: "and", clauses: [] }, ctx)).toBe(true);
  });
});

describe("helpers", () => {
  it("isValidPredicate is the boolean of predicateErrors", () => {
    expect(isValidPredicate({ op: "eq", field: "a", value: 1 })).toBe(true);
    expect(isValidPredicate({ op: "eq" })).toBe(false);
  });

  it("opsForDataType offers type-appropriate ops", () => {
    expect(opsForDataType("number")).toContain("gt");
    expect(opsForDataType("boolean")).not.toContain("gt");
    expect(opsForDataType("list")).toContain("contains");
    expect(opsForDataType("string")).toContain("eq");
  });
});
