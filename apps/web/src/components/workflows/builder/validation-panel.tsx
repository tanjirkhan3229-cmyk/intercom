"use client";

import { cn } from "@/lib/utils";
import type { GraphError } from "@/lib/workflows/contract";

/** Collapsible list of validation problems; clicking a row focuses the offending node on the canvas. */
export function ValidationPanel({
  errors,
  onFocusNode,
}: {
  errors: GraphError[];
  onFocusNode: (nodeId: string) => void;
}) {
  if (errors.length === 0) {
    return (
      <div className="border-t border-border px-4 py-2 text-xs text-emerald-600" data-testid="validation-panel">
        ✓ Ready to publish
      </div>
    );
  }
  return (
    <div
      className="max-h-40 overflow-y-auto border-t border-border"
      data-testid="validation-panel"
    >
      <ul className="divide-y divide-border">
        {errors.map((e, i) => (
          <li key={i}>
            <button
              type="button"
              disabled={!e.nodeId}
              onClick={() => e.nodeId && onFocusNode(e.nodeId)}
              data-testid="validation-row"
              className={cn(
                "flex w-full items-center gap-2 px-4 py-1.5 text-left text-xs",
                e.nodeId && "hover:bg-accent/50",
              )}
            >
              <span
                className={cn(
                  "inline-block h-1.5 w-1.5 shrink-0 rounded-full",
                  e.severity === "error" ? "bg-destructive" : "bg-amber-500",
                )}
              />
              <span className={cn(e.severity === "error" ? "text-foreground" : "text-muted-foreground")}>
                {e.message}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
