"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import type { Route } from "next";
import { Button } from "@/components/ui/button";
import { Badge, Spinner } from "@/components/ui/primitives";
import { EmptyState, ErrorState, LoadingState } from "@/components/inbox/states";
import { timeAgo } from "@/lib/format";
import { flattenWorkflows, useCreateWorkflow, useWorkflows } from "@/lib/workflows/workflows-hooks";
import type { WorkflowSummary } from "@/lib/workflows/contract";

/** Workflow list + create (P1.6). "New workflow" creates an empty draft and jumps into the builder. */
export default function WorkflowsPage() {
  const router = useRouter();
  const workflows = useWorkflows();
  const create = useCreateWorkflow();
  const items = React.useMemo(() => flattenWorkflows(workflows.data), [workflows.data]);

  const onNew = async () => {
    const created = await create.mutateAsync("Untitled workflow");
    router.push(`/app/workflows/${created.id}` as Route);
  };

  return (
    <div className="flex h-screen flex-col bg-background">
      <header className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
        <div className="flex items-center gap-3">
          <Link
            href={"/app" as Route}
            className="text-xs font-medium text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
          >
            ← Inbox
          </Link>
          <h1 className="text-sm font-semibold">Workflows</h1>
        </div>
        <Button size="sm" data-testid="new-workflow" onClick={() => void onNew()} disabled={create.isPending}>
          {create.isPending ? <Spinner className="h-3.5 w-3.5" /> : "New workflow"}
        </Button>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {workflows.isLoading ? (
          <LoadingState label="Loading workflows…" />
        ) : workflows.isError ? (
          <ErrorState error={workflows.error} onRetry={() => void workflows.refetch()} />
        ) : items.length === 0 ? (
          <EmptyState title="No workflows yet" hint='Click "New workflow" to build your first automation.' />
        ) : (
          <ul className="divide-y divide-border" data-testid="workflow-list">
            {items.map((wf) => (
              <WorkflowRow key={wf.id} workflow={wf} />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function WorkflowRow({ workflow }: { workflow: WorkflowSummary }) {
  return (
    <li>
      <Link
        href={`/app/workflows/${workflow.id}` as Route}
        className="flex items-center gap-3 px-4 py-3 transition-colors hover:bg-accent/50"
        data-testid="workflow-row"
      >
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium">{workflow.name}</p>
          <p className="mt-0.5 text-xs text-muted-foreground">
            {workflow.active_version ? `v${workflow.active_version} live` : "not published"}
          </p>
        </div>
        {workflow.active_runs_on_old_versions > 0 && (
          <Badge variant="muted" data-testid="old-versions-badge">
            {workflow.active_runs_on_old_versions} on old version
          </Badge>
        )}
        <Badge
          variant={workflow.status === "active" ? "default" : "muted"}
          className="shrink-0 capitalize"
        >
          {workflow.status}
        </Badge>
        <span className="shrink-0 text-[11px] text-muted-foreground">
          {timeAgo(workflow.updated_at)}
        </span>
      </Link>
    </li>
  );
}
