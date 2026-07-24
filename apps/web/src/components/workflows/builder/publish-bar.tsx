"use client";

import { Button } from "@/components/ui/button";
import { Badge, Spinner } from "@/components/ui/primitives";

export type SaveState = "idle" | "unsaved" | "saving" | "saved" | "error";

/** Top bar: draft save indicator, validation summary, and the Publish action (disabled while the
 * graph has blocking errors — P1.6 acceptance #2). */
export function PublishBar({
  saveState,
  errorCount,
  warningCount,
  publishing,
  onPublish,
}: {
  saveState: SaveState;
  errorCount: number;
  warningCount: number;
  publishing: boolean;
  onPublish: () => void;
}) {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-2">
      <div className="flex items-center gap-3 text-xs text-muted-foreground" data-testid="save-state">
        {saveState === "unsaved" && <span>Unsaved changes…</span>}
        {saveState === "saving" && (
          <span className="flex items-center gap-1">
            <Spinner className="h-3 w-3" /> Saving…
          </span>
        )}
        {saveState === "saved" && <span>All changes saved</span>}
        {saveState === "error" && <span className="text-destructive">Save failed — retrying</span>}
      </div>

      <div className="flex items-center gap-3">
        {errorCount > 0 && (
          <Badge variant="outline" className="border-destructive/50 text-destructive" data-testid="error-count">
            {errorCount} {errorCount === 1 ? "error" : "errors"}
          </Badge>
        )}
        {warningCount > 0 && (
          <Badge variant="muted" data-testid="warning-count">
            {warningCount} {warningCount === 1 ? "warning" : "warnings"}
          </Badge>
        )}
        <Button
          type="button"
          size="sm"
          data-testid="publish"
          disabled={errorCount > 0 || publishing}
          onClick={onPublish}
        >
          {publishing ? <Spinner className="h-3.5 w-3.5" /> : "Publish"}
        </Button>
      </div>
    </div>
  );
}
