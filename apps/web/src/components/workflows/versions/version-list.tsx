"use client";

import { timeAgo } from "@/lib/format";
import { Badge, Spinner } from "@/components/ui/primitives";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { ErrorState, LoadingState } from "@/components/inbox/states";
import { flattenVersions, useWorkflow, useWorkflowVersions } from "@/lib/workflows/workflows-hooks";

/** Published version history, with the active version marked and a "runs on old versions" banner
 * (proves in-flight runs stay pinned across a publish — P1.6 acceptance #3). */
export function VersionList({ workflowId }: { workflowId: string }) {
  const workflow = useWorkflow(workflowId);
  const versions = useWorkflowVersions(workflowId);
  const items = flattenVersions(versions.data).filter((v) => v.status !== "draft");
  const activeId = workflow.data?.active_version_id ?? null;
  const oldRunCount = workflow.data?.active_runs_on_old_versions ?? 0;

  if (versions.isLoading) return <LoadingState label="Loading versions…" />;
  if (versions.isError) {
    return <ErrorState error={versions.error} onRetry={() => void versions.refetch()} />;
  }

  return (
    <div className="flex flex-col" data-testid="version-list">
      {oldRunCount > 0 && (
        <p
          className="border-b border-border bg-amber-50 px-4 py-2 text-xs text-amber-700"
          data-testid="old-version-runs"
        >
          {oldRunCount} in-flight {oldRunCount === 1 ? "run is" : "runs are"} still on an older
          version.
        </p>
      )}
      {items.length === 0 ? (
        <p className="px-4 py-3 text-xs text-muted-foreground">No published versions yet.</p>
      ) : (
        <ul className="divide-y divide-border">
          {items.map((v) => (
            <li
              key={v.id}
              data-testid="version-row"
              className={cn(
                "flex items-center gap-3 px-4 py-2.5",
                v.id === activeId && "bg-accent/40",
              )}
            >
              <span className="text-sm font-medium">v{v.version}</span>
              {v.id === activeId && (
                <Badge variant="default" data-testid="version-active">
                  active
                </Badge>
              )}
              <span className="ml-auto text-[11px] text-muted-foreground">
                {timeAgo(v.created_at)}
              </span>
            </li>
          ))}
        </ul>
      )}
      {versions.hasNextPage && (
        <div className="p-3 text-center">
          <Button
            variant="outline"
            size="sm"
            disabled={versions.isFetchingNextPage}
            onClick={() => void versions.fetchNextPage()}
          >
            {versions.isFetchingNextPage ? <Spinner className="h-3.5 w-3.5" /> : "Load more"}
          </Button>
        </div>
      )}
    </div>
  );
}
