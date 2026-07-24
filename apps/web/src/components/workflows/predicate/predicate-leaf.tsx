"use client";

import { Button } from "@/components/ui/button";
import { Input, Select } from "@/components/ui/primitives";
import type {
  ComparisonOp,
  Predicate,
  PredicateOp,
  PredicateScalar,
} from "@/lib/workflows/contract";
import { isPresenceOp, opsForDataType } from "@/lib/workflows/predicate";
import { FieldPicker } from "./field-picker";
import { dataTypeForPath, type FieldOption } from "./fields";

/** A single comparison predicate: `{op, field, value?}` (never a group). */
export type LeafPredicate = Extract<Predicate, { field: string }>;

const OP_LABELS: Record<PredicateOp, string> = {
  and: "all of",
  or: "any of",
  not: "not",
  eq: "is",
  ne: "is not",
  gt: ">",
  gte: "≥",
  lt: "<",
  lte: "≤",
  in: "is one of",
  contains: "contains",
  exists: "is set",
  not_exists: "is not set",
};

function coerce(raw: string, dataType: string): PredicateScalar {
  if (dataType === "number") {
    const n = Number(raw);
    return raw.trim() !== "" && Number.isFinite(n) ? n : raw;
  }
  if (dataType === "boolean") return raw === "true";
  return raw;
}

export function PredicateLeaf({
  value,
  options,
  onChange,
  onRemove,
}: {
  value: LeafPredicate;
  options: FieldOption[];
  onChange: (next: LeafPredicate) => void;
  onRemove: () => void;
}) {
  const dataType = dataTypeForPath(value.field, options);
  const ops = opsForDataType(dataType);

  const changeField = (field: string) => onChange({ ...value, field } as LeafPredicate);

  const changeOp = (op: ComparisonOp) => {
    if (isPresenceOp(op)) {
      onChange({ op, field: value.field });
    } else if (op === "in") {
      const arr = "value" in value && Array.isArray(value.value) ? value.value : [];
      onChange({ op, field: value.field, value: arr });
    } else {
      const scalar =
        "value" in value && !Array.isArray(value.value) ? (value.value as PredicateScalar) : "";
      onChange({ op, field: value.field, value: scalar });
    }
  };

  const scalarValue = "value" in value && !Array.isArray(value.value) ? value.value : "";
  const listValue =
    "value" in value && Array.isArray(value.value) ? value.value.join(", ") : "";

  return (
    <div className="flex items-start gap-2" data-testid="predicate-leaf">
      <FieldPicker value={value.field} options={options} onChange={changeField} />
      <Select
        aria-label="Operator"
        data-testid="predicate-op"
        className="w-32 shrink-0"
        value={value.op}
        onChange={(e) => changeOp(e.target.value as ComparisonOp)}
      >
        {ops.map((op) => (
          <option key={op} value={op}>
            {OP_LABELS[op]}
          </option>
        ))}
      </Select>

      {isPresenceOp(value.op) ? null : value.op === "in" ? (
        <Input
          aria-label="Values (comma separated)"
          data-testid="predicate-value"
          className="w-40 shrink-0"
          placeholder="a, b, c"
          value={listValue}
          onChange={(e) =>
            onChange({
              op: "in",
              field: value.field,
              value: e.target.value
                .split(",")
                .map((s) => s.trim())
                .filter((s) => s.length > 0)
                .map((s) => coerce(s, dataType)),
            })
          }
        />
      ) : dataType === "boolean" ? (
        <Select
          aria-label="Value"
          data-testid="predicate-value"
          className="w-40 shrink-0"
          value={String(scalarValue)}
          onChange={(e) => onChange({ ...value, value: e.target.value === "true" } as LeafPredicate)}
        >
          <option value="true">true</option>
          <option value="false">false</option>
        </Select>
      ) : (
        <Input
          aria-label="Value"
          data-testid="predicate-value"
          className="w-40 shrink-0"
          type={dataType === "number" ? "number" : dataType === "date" ? "date" : "text"}
          value={String(scalarValue ?? "")}
          onChange={(e) =>
            onChange({ ...value, value: coerce(e.target.value, dataType) } as LeafPredicate)
          }
        />
      )}

      <Button
        type="button"
        variant="ghost"
        size="icon"
        aria-label="Remove condition"
        onClick={onRemove}
      >
        ✕
      </Button>
    </div>
  );
}
