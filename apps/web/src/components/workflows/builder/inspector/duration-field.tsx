"use client";

import { Input, Label, Select } from "@/components/ui/primitives";
import type { DurationParams } from "@/lib/workflows/contract";

/** Editor for the shared wait/snooze duration shape: a positive `seconds` OR an ISO `until`. */
export function DurationField({
  value,
  onChange,
}: {
  value: DurationParams;
  onChange: (next: DurationParams) => void;
}) {
  // Prefer a present `seconds` so a malformed params object carrying both keys can't hide a valid
  // duration behind an empty "until" field.
  const mode: "seconds" | "until" =
    "seconds" in value && value.seconds != null ? "seconds" : "until" in value ? "until" : "seconds";
  return (
    <div className="flex flex-col gap-2">
      <div>
        <Label>Wait by</Label>
        <Select
          data-testid="duration-mode"
          value={mode}
          onChange={(e) =>
            onChange(e.target.value === "until" ? { until: "" } : { seconds: 3600 })
          }
        >
          <option value="seconds">Duration (seconds)</option>
          <option value="until">Until a date/time</option>
        </Select>
      </div>
      {mode === "seconds" ? (
        <div>
          <Label>Seconds</Label>
          <Input
            data-testid="duration-seconds"
            type="number"
            min={1}
            value={"seconds" in value ? value.seconds : 3600}
            onChange={(e) => onChange({ seconds: Math.max(1, Math.floor(Number(e.target.value) || 0)) })}
          />
        </div>
      ) : (
        <div>
          <Label>Until (ISO-8601)</Label>
          <Input
            data-testid="duration-until"
            placeholder="2026-08-01T09:00:00Z"
            value={"until" in value ? value.until : ""}
            onChange={(e) => onChange({ until: e.target.value })}
          />
        </div>
      )}
    </div>
  );
}
