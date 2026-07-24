"use client";

import Link from "next/link";
import type { Route } from "next";
import { useParams } from "next/navigation";
import { ErrorState } from "@/components/inbox/states";
import { RunList } from "@/components/workflows/runs/run-list";
import { VersionList } from "@/components/workflows/versions/version-list";

/** Runs + version history for a workflow. */
export default function WorkflowRunsPage() {
  const params = useParams<{ id: string }>();
  const id = params?.id;
  if (!id) return <ErrorState title="Missing workflow id" />;

  return (
    <div className="flex h-screen flex-col bg-background">
      <header className="flex items-center gap-3 border-b border-border px-4 py-3">
        <Link
          href={`/app/workflows/${id}` as Route}
          className="text-xs font-medium text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
        >
          ← Builder
        </Link>
        <h1 className="text-sm font-semibold">Runs</h1>
      </header>

      <div className="flex min-h-0 flex-1">
        <aside className="w-72 shrink-0 overflow-y-auto border-r border-border">
          <div className="border-b border-border px-4 py-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            Versions
          </div>
          <VersionList workflowId={id} />
        </aside>
        <main className="min-w-0 flex-1 overflow-y-auto">
          <RunList workflowId={id} />
        </main>
      </div>
    </div>
  );
}
