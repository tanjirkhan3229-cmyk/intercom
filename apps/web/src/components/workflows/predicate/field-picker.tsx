"use client";

import * as React from "react";
import { Input, Select } from "@/components/ui/primitives";
import type { FieldOption } from "./fields";

const CUSTOM = "__custom__";

/**
 * Field selector for a predicate leaf. Lists the known run-context fields + contact attributes, plus
 * a "Custom field…" escape hatch that reveals a free-text dotted-path input (so authors aren't
 * limited to the catalog while the executor's context shape settles).
 */
export function FieldPicker({
  value,
  options,
  onChange,
}: {
  value: string;
  options: FieldOption[];
  onChange: (path: string) => void;
}) {
  const known = options.some((o) => o.path === value);
  // `manual` tracks an explicit "Custom field…" choice; combined with a *derived* check so that when
  // the attribute list loads later and the value becomes known, the dropdown reconciles (fixes the
  // "stuck in custom-text mode" bug) instead of freezing an initial useState.
  const [manual, setManual] = React.useState(false);
  const showCustom = manual || (value.length > 0 && !known);

  return (
    <div className="flex flex-1 flex-col gap-1">
      <Select
        aria-label="Field"
        data-testid="predicate-field"
        value={showCustom ? CUSTOM : value}
        onChange={(e) => {
          const v = e.target.value;
          if (v === CUSTOM) {
            setManual(true);
          } else {
            setManual(false);
            onChange(v);
          }
        }}
      >
        <option value="" disabled>
          Select a field…
        </option>
        {options.map((o) => (
          <option key={o.path} value={o.path}>
            {o.label}
          </option>
        ))}
        <option value={CUSTOM}>Custom field…</option>
      </Select>
      {showCustom && (
        <Input
          aria-label="Custom field path"
          data-testid="predicate-field-custom"
          placeholder="e.g. contact.custom.plan"
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
    </div>
  );
}
