"use client";

import { Button } from "@/components/ui/button";
import { Badge, Select } from "@/components/ui/primitives";
import type { Predicate } from "@/lib/workflows/contract";
import { PredicateLeaf, type LeafPredicate } from "./predicate-leaf";
import { useFieldOptions, type FieldOption } from "./fields";

type GroupPredicate = { op: "and" | "or"; clauses: Predicate[] };

function isGroup(p: Predicate | undefined): p is GroupPredicate {
  return !!p && (p.op === "and" || p.op === "or");
}
function isNot(p: Predicate): p is { op: "not"; clause: Predicate } {
  return p.op === "not";
}

function defaultLeaf(options: FieldOption[]): LeafPredicate {
  return { op: "eq", field: options[0]?.path ?? "", value: "" };
}

/**
 * Recursive predicate builder emitting the exact `core/predicates.py` AST. The root is presented as
 * an all/any group; leaves can be negated (wrapped in `not`) and groups can nest. Reused later by
 * P1.9 segments — it is the canonical predicate UI.
 */
export function PredicateEditor({
  value,
  onChange,
}: {
  value: Predicate | undefined;
  onChange: (next: Predicate) => void;
}) {
  const { options } = useFieldOptions();
  const root: GroupPredicate = isGroup(value)
    ? value
    : { op: "and", clauses: value ? [value] : [] };
  return <GroupEditor group={root} options={options} onChange={onChange} />;
}

function GroupEditor({
  group,
  options,
  onChange,
}: {
  group: GroupPredicate;
  options: FieldOption[];
  onChange: (next: GroupPredicate) => void;
}) {
  const clauses = group.clauses;
  const setClause = (i: number, c: Predicate) =>
    onChange({ ...group, clauses: clauses.map((x, idx) => (idx === i ? c : x)) });
  const removeAt = (i: number) =>
    onChange({ ...group, clauses: clauses.filter((_, idx) => idx !== i) });

  return (
    <div className="flex flex-col gap-2" data-testid="predicate-group">
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        Match
        <Select
          aria-label="Match all or any"
          className="h-7 w-24"
          value={group.op}
          onChange={(e) => onChange({ ...group, op: e.target.value as "and" | "or" })}
        >
          <option value="and">all</option>
          <option value="or">any</option>
        </Select>
        of the following:
      </div>

      {clauses.length === 0 && (
        <p className="text-xs italic text-muted-foreground">No conditions yet.</p>
      )}

      <div className="flex flex-col gap-2">
        {clauses.map((clause, i) => (
          <ClauseEditor
            key={i}
            clause={clause}
            options={options}
            onChange={(c) => setClause(i, c)}
            onRemove={() => removeAt(i)}
          />
        ))}
      </div>

      <div className="flex gap-2">
        <Button
          type="button"
          variant="outline"
          size="sm"
          data-testid="predicate-add-condition"
          onClick={() => onChange({ ...group, clauses: [...clauses, defaultLeaf(options)] })}
        >
          + Condition
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          data-testid="predicate-add-group"
          onClick={() => onChange({ ...group, clauses: [...clauses, { op: "and", clauses: [] }] })}
        >
          + Group
        </Button>
      </div>
    </div>
  );
}

function ClauseEditor({
  clause,
  options,
  onChange,
  onRemove,
}: {
  clause: Predicate;
  options: FieldOption[];
  onChange: (next: Predicate) => void;
  onRemove: () => void;
}) {
  const negated = isNot(clause);
  const inner: Predicate = negated ? clause.clause : clause;
  const wrap = (next: Predicate) => onChange(negated ? { op: "not", clause: next } : next);
  const toggleNegate = () => onChange(negated ? inner : { op: "not", clause: inner });

  if (isGroup(inner)) {
    return (
      <div className="rounded-md border border-border p-2">
        <div className="mb-2 flex items-center gap-2">
          {negated && <Badge variant="muted">NOT</Badge>}
          <Button type="button" variant="ghost" size="sm" onClick={toggleNegate}>
            {negated ? "un-negate" : "negate"}
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="ml-auto"
            onClick={onRemove}
            aria-label="Remove group"
          >
            ✕
          </Button>
        </div>
        <GroupEditor group={inner} options={options} onChange={(g) => wrap(g)} />
      </div>
    );
  }

  return (
    <div className="flex items-start gap-2">
      <Button
        type="button"
        size="sm"
        variant={negated ? "default" : "outline"}
        className="mt-0.5 shrink-0"
        title="Negate this condition"
        onClick={toggleNegate}
      >
        not
      </Button>
      <PredicateLeaf
        value={inner as LeafPredicate}
        options={options}
        onChange={(l) => wrap(l)}
        onRemove={onRemove}
      />
    </div>
  );
}
