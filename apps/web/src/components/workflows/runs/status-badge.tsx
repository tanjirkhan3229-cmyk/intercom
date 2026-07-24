"use client";

import { Badge } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import type { RunStatus, StepStatus } from "@/lib/workflows/contract";

const RUN_TONE: Record<RunStatus, string> = {
  running: "bg-sky-100 text-sky-700",
  waiting: "bg-amber-100 text-amber-700",
  suspended: "bg-amber-100 text-amber-700",
  awaiting_input: "bg-violet-100 text-violet-700",
  completed: "bg-emerald-100 text-emerald-700",
  failed: "bg-destructive/10 text-destructive",
  cancelled: "bg-muted text-muted-foreground",
};

const STEP_TONE: Record<StepStatus, string> = {
  started: "bg-sky-100 text-sky-700",
  done: "bg-emerald-100 text-emerald-700",
  failed: "bg-destructive/10 text-destructive",
  skipped: "bg-muted text-muted-foreground",
};

export function RunStatusBadge({
  status,
  testId = "run-status",
}: {
  status: RunStatus;
  testId?: string;
}) {
  return (
    <Badge className={cn("capitalize", RUN_TONE[status])} data-testid={testId}>
      {status.replace("_", " ")}
    </Badge>
  );
}

export function StepStatusBadge({ status }: { status: StepStatus }) {
  return (
    <Badge className={cn("capitalize", STEP_TONE[status])} data-testid="step-status">
      {status}
    </Badge>
  );
}
