"use client";

import Link from "next/link";
import type { Route } from "next";
import { useParams } from "next/navigation";
import { ErrorState } from "@/components/inbox/states";
import { RunTimeline } from "@/components/workflows/runs/run-timeline";

/** Single run log (step timeline + re-run from a failed step). */
export default function WorkflowRunPage() {
  const params = useParams<{ id: string; runId: string }>();
  const id = params?.id;
  const runId = params?.runId;
  if (!id || !runId) return <ErrorState title="Missing run id" />;

  return (
    <div className="flex h-screen flex-col bg-background">
      <header className="flex items-center gap-3 border-b border-border px-4 py-3">
        <Link
          href={`/app/workflows/${id}/runs` as Route}
          className="text-xs font-medium text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
        >
          ← Runs
        </Link>
        <h1 className="text-sm font-semibold">Run log</h1>
      </header>
      <div className="min-h-0 flex-1">
        <RunTimeline runId={runId} />
      </div>
    </div>
  );
}
