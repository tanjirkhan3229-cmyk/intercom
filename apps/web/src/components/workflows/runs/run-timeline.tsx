"use client";

import { timeAgo } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/primitives";
import { ErrorState, LoadingState } from "@/components/inbox/states";
import type { StepStatus } from "@/lib/workflows/contract";
import {
  useRerunFromStep,
  useWorkflowRun,
  useWorkflowRunSteps,
} from "@/lib/workflows/workflows-hooks";
import { RunStatusBadge, StepStatusBadge } from "./status-badge";

const STEP_DOT: Record<StepStatus, string> = {
  started: "bg-sky-500",
  done: "bg-emerald-500",
  failed: "bg-destructive",
  skipped: "bg-muted-foreground/50",
};

/** The run log: pinned-version header + the step ledger timeline, with a "re-run from here" action
 * on failed steps (idempotent-effect steps only). */
export function RunTimeline({ runId }: { runId: string }) {
  const run = useWorkflowRun(runId);
  const steps = useWorkflowRunSteps(runId);
  const rerun = useRerunFromStep(runId);

  if (run.isLoading || steps.isLoading) return <LoadingState label="Loading run…" />;
  if (run.isError) return <ErrorState error={run.error} onRetry={() => void run.refetch()} />;
  if (!run.data) return null;

  const items = steps.data ?? [];

  return (
    <div className="flex h-full flex-col" data-testid="run-timeline">
      <header className="border-b border-border px-4 py-3">
        <div className="flex items-center gap-3">
          <RunStatusBadge status={run.data.status} testId="run-detail-status" />
          <span className="text-xs text-muted-foreground" data-testid="run-detail-version">
            version {run.data.version}
          </span>
          <span className="text-xs text-muted-foreground">{run.data.trigger_topic}</span>
          <span className="ml-auto text-[11px] text-muted-foreground">
            {timeAgo(run.data.created_at)}
          </span>
        </div>
        {run.data.error && (
          <p className="mt-2 rounded-md bg-destructive/10 px-2 py-1 text-xs text-destructive">
            {run.data.error}
          </p>
        )}
      </header>

      <ol className="min-h-0 flex-1 overflow-y-auto p-4" data-testid="run-steps">
        {items.length === 0 && (
          <li className="text-xs text-muted-foreground">No steps recorded yet.</li>
        )}
        {items.map((step) => (
          <li
            key={step.id}
            data-testid="run-step"
            data-node-id={step.node_id}
            className="relative ml-1 flex items-start gap-3 border-l-2 border-border py-2 pl-5"
          >
            <span
              aria-hidden
              className={cn(
                "absolute -left-[5px] top-3 h-2 w-2 rounded-full ring-2 ring-background",
                STEP_DOT[step.status],
              )}
            />
            <StepStatusBadge status={step.status} />
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium">
                {step.node_type ?? "step"}{" "}
                <span className="text-xs font-normal text-muted-foreground">({step.node_id})</span>
              </p>
              {step.action_type && (
                <p className="text-xs text-muted-foreground">action: {step.action_type}</p>
              )}
              {step.error && <p className="text-xs text-destructive">{step.error}</p>}
            </div>
            {step.status === "failed" && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                data-testid="rerun-from-step"
                disabled={rerun.isPending}
                onClick={() => rerun.mutate(step.node_id)}
              >
                {rerun.isPending ? <Spinner className="h-3.5 w-3.5" /> : "Re-run from here"}
              </Button>
            )}
          </li>
        ))}
      </ol>
    </div>
  );
}
