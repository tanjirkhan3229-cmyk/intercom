/**
 * Predicate validation + evaluation — a faithful TypeScript mirror of
 * `apps/api/src/relay/core/predicates.py`.
 *
 * - `predicateErrors` collects *all* problems (rich UI), where the Python `validate_predicate`
 *   raises on the first. The graph validator surfaces every issue; the server remains the gate.
 * - `evaluate` is the total, side-effect-free evaluator the e2e mock's mini-executor uses to walk
 *   condition branches — matching the backend so a graph behaves identically in the mock.
 */

import {
  ALL_PREDICATE_OPS,
  PREDICATE_MAX_DEPTH,
  PRESENCE_OPS,
  VALUE_OPS,
  type ComparisonOp,
  type PredicateOp,
  type PresenceOp,
  type ValueOp,
} from "./contract";

export interface PredicateProblem {
  path: string;
  message: string;
}

const ALL_OPS = new Set<string>(ALL_PREDICATE_OPS);
const PRESENCE = new Set<string>(PRESENCE_OPS);
const VALUE = new Set<string>(VALUE_OPS);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Collect all validation problems in a predicate subtree (mirror of `validate_predicate`). */
export function predicateErrors(node: unknown, path = "predicate", depth = 0): PredicateProblem[] {
  const errs: PredicateProblem[] = [];
  if (depth > PREDICATE_MAX_DEPTH) {
    return [{ path, message: "predicate nested too deeply" }];
  }
  if (!isRecord(node)) {
    return [{ path, message: "predicate must be an object" }];
  }
  const op = node.op;
  if (typeof op !== "string" || !ALL_OPS.has(op)) {
    return [{ path, message: `unknown predicate op ${JSON.stringify(op)}` }];
  }

  if (op === "and" || op === "or") {
    const clauses = node.clauses;
    if (!Array.isArray(clauses)) {
      return [{ path: `${path}.clauses`, message: `'${op}' requires a 'clauses' list` }];
    }
    clauses.forEach((clause, i) => {
      errs.push(...predicateErrors(clause, `${path}.clauses[${i}]`, depth + 1));
    });
    return errs;
  }
  if (op === "not") {
    if (!("clause" in node)) {
      return [{ path, message: "'not' requires a 'clause'" }];
    }
    return predicateErrors(node.clause, `${path}.clause`, depth + 1);
  }

  // Leaf comparison ops all require a non-empty string `field`.
  const field = node.field;
  if (typeof field !== "string" || field.length === 0) {
    return [{ path, message: `'${op}' requires a non-empty string 'field'` }];
  }
  if (PRESENCE.has(op)) return errs;
  if (!("value" in node)) {
    return [{ path, message: `'${op}' requires a 'value'` }];
  }
  if (op === "in" && !Array.isArray(node.value)) {
    return [{ path, message: "'in' requires a list 'value'" }];
  }
  return errs;
}

/** True when the predicate subtree is well-formed. */
export function isValidPredicate(node: unknown): boolean {
  return predicateErrors(node).length === 0;
}

// --- Evaluation (mirror of `predicates.py.evaluate`) --------------------------

const MISSING = Symbol("missing");

function resolveField(context: Record<string, unknown>, field: string): unknown {
  let cur: unknown = context;
  for (const part of field.split(".")) {
    if (!isRecord(cur) || !(part in cur)) return MISSING;
    cur = cur[part];
  }
  return cur;
}

function compare(op: ValueOp, left: unknown, right: unknown): boolean {
  if (left === MISSING || left === null) return false;
  // Only order-compare like-typed scalars; mismatched types are "not comparable" → false
  // (mirrors Python raising TypeError, which the evaluator swallows to False).
  const bothNumbers = typeof left === "number" && typeof right === "number";
  const bothStrings = typeof left === "string" && typeof right === "string";
  if (!bothNumbers && !bothStrings) return false;
  switch (op) {
    case "gt":
      return left > right;
    case "gte":
      return left >= right;
    case "lt":
      return left < right;
    case "lte":
      return left <= right;
    default:
      return false;
  }
}

/** Evaluate a (validated) predicate against a flat-ish context. Total + side-effect free. */
export function evaluate(node: unknown, context: Record<string, unknown>): boolean {
  if (!isRecord(node)) return false;
  const op = node.op as PredicateOp | undefined;

  if (op === "and") {
    const clauses = Array.isArray(node.clauses) ? node.clauses : [];
    return clauses.every((c) => evaluate(c, context));
  }
  if (op === "or") {
    const clauses = Array.isArray(node.clauses) ? node.clauses : [];
    return clauses.some((c) => evaluate(c, context));
  }
  if (op === "not") {
    return isRecord(node.clause) ? !evaluate(node.clause, context) : false;
  }

  const field = node.field;
  if (typeof field !== "string") return false;
  const left = resolveField(context, field);

  if (op === "exists") return left !== MISSING && left !== null;
  if (op === "not_exists") return left === MISSING || left === null;

  const value = node.value;
  if (op === "eq") return left !== MISSING && left === value;
  if (op === "ne") return left === MISSING || left !== value;
  if (op === "in") return left !== MISSING && Array.isArray(value) && value.includes(left);
  if (op === "contains") {
    if (left === MISSING || left === null) return false;
    if (typeof left === "string") return typeof value === "string" && left.includes(value);
    if (Array.isArray(left)) return left.includes(value);
    return false;
  }
  if (op && VALUE.has(op)) return compare(op as ValueOp, left, value);
  return false;
}

/** Ops applicable to a given attribute data type (drives the leaf editor's op dropdown). */
export function opsForDataType(dataType: string): ComparisonOp[] {
  switch (dataType) {
    case "number":
    case "date":
      return ["eq", "ne", "gt", "gte", "lt", "lte", "exists", "not_exists"];
    case "boolean":
      return ["eq", "ne", "exists", "not_exists"];
    case "list":
      return ["contains", "in", "exists", "not_exists"];
    case "string":
    default:
      return ["eq", "ne", "contains", "in", "exists", "not_exists"];
  }
}

export function isPresenceOp(op: string): op is PresenceOp {
  return PRESENCE.has(op);
}
