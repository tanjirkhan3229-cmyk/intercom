"use client";

import Link from "next/link";
import type { Route } from "next";
import { timeAgo } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/primitives";
import { EmptyState, ErrorState, LoadingState } from "@/components/inbox/states";
import { flattenRuns, useWorkflowRuns } from "@/lib/workflows/workflows-hooks";
import { RunStatusBadge } from "./status-badge";

/** Runs for a workflow, newest first. Each row shows the pinned version + status and links to the
 * run log. */
export function RunList({ workflowId }: { workflowId: string }) {
  const runs = useWorkflowRuns(workflowId);
  const items = flattenRuns(runs.data);

  if (runs.isLoading) return <LoadingState label="Loading runs…" />;
  if (runs.isError) return <ErrorState error={runs.error} onRetry={() => void runs.refetch()} />;
  if (items.length === 0) {
    return <EmptyState title="No runs yet" hint="Runs appear here once the workflow fires." />;
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <ul className="divide-y divide-border" data-testid="run-list">
        {items.map((run) => (
          <li key={run.id}>
            <Link
              href={`/app/workflows/${run.workflow_id}/runs/${run.id}` as Route}
              className="flex items-center gap-3 px-4 py-3 transition-colors hover:bg-accent/50"
              data-testid="run-row"
            >
              <RunStatusBadge status={run.status} />
              <span className="text-xs text-muted-foreground" data-testid="run-version">
                v{run.version}
              </span>
              <span className="min-w-0 flex-1 truncate text-sm">{run.trigger_topic}</span>
              <span className="shrink-0 text-[11px] text-muted-foreground">
                {timeAgo(run.created_at)}
              </span>
            </Link>
          </li>
        ))}
      </ul>
      {runs.hasNextPage && (
        <div className="p-3 text-center">
          <Button
            variant="outline"
            size="sm"
            disabled={runs.isFetchingNextPage}
            onClick={() => void runs.fetchNextPage()}
          >
            {runs.isFetchingNextPage ? <Spinner className="h-3.5 w-3.5" /> : "Load more"}
          </Button>
        </div>
      )}
    </div>
  );
}
