"use client";

import * as React from "react";
import { useSources, useSourceMutations } from "@/lib/hc-hooks";
import { Button } from "@/components/ui/button";
import { Input, Textarea, Badge, Spinner } from "@/components/ui/primitives";
import { LoadingState, ErrorState } from "@/components/inbox/states";
import { timeAgo } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { Source, SourceInput, SourceKind, SourceStatus } from "@/lib/types";

/**
 * Knowledge Hub sources manager (P1.1): add url/pdf/snippet sources, trigger a sync, and — the
 * point of this panel — surface each source's **AI-readiness** status (pending/syncing/synced/error)
 * so an admin can see what Neko can actually retrieve from. The list auto-polls while anything is
 * ingesting (see `useSources`).
 */
export function SourcesPanel() {
  const query = useSources();
  const { create } = useSourceMutations();
  const [kind, setKind] = React.useState<SourceKind>("url");
  const [title, setTitle] = React.useState("");
  const [value, setValue] = React.useState(""); // url | s3_key | snippet body

  const valid = title.trim() !== "" && value.trim() !== "";

  const onCreate = () => {
    if (!valid) return;
    const config: Record<string, string> =
      kind === "url" ? { url: value.trim() } : kind === "pdf" ? { s3_key: value.trim() } : { body: value };
    const input: SourceInput = { kind, title: title.trim(), config };
    create.mutate(input);
    setTitle("");
    setValue("");
  };

  return (
    <div className="flex flex-col gap-3" data-testid="sources-panel">
      <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        AI sources
      </p>

      <form
        className="flex flex-col gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          onCreate();
        }}
      >
        <div className="flex items-center gap-2">
          <select
            value={kind}
            onChange={(e) => setKind(e.target.value as SourceKind)}
            aria-label="Source kind"
            className="h-8 rounded-md border border-input bg-background px-2 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          >
            <option value="url">URL</option>
            <option value="pdf">PDF</option>
            <option value="snippet">Snippet</option>
          </select>
          <Input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Title"
            className="h-8"
          />
        </div>
        {kind === "snippet" ? (
          <Textarea
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="Snippet text Neko can answer from"
            rows={3}
          />
        ) : (
          <Input
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={kind === "url" ? "https://docs.example.com" : "S3 key of the PDF"}
            className="h-8"
          />
        )}
        <Button type="submit" size="sm" disabled={!valid || create.isPending} className="self-start">
          {create.isPending ? <Spinner className="h-3.5 w-3.5" /> : "Add source"}
        </Button>
      </form>

      {query.isLoading ? (
        <LoadingState label="Loading sources…" className="h-24" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} className="h-24" />
      ) : query.data && query.data.length > 0 ? (
        <ul className="flex flex-col gap-1">
          {query.data.map((s) => (
            <SourceRow key={s.id} source={s} />
          ))}
        </ul>
      ) : (
        <p className="text-xs text-muted-foreground">No sources yet.</p>
      )}
    </div>
  );
}

const STATUS_STYLE: Record<SourceStatus, { variant: "default" | "muted"; className: string; label: string }> = {
  pending: { variant: "muted", className: "", label: "Pending" },
  syncing: { variant: "muted", className: "animate-pulse", label: "Ingesting" },
  synced: { variant: "default", className: "", label: "Ready" },
  error: { variant: "muted", className: "bg-destructive/10 text-destructive", label: "Error" },
};

function StatusBadge({ status }: { status: SourceStatus }) {
  const s = STATUS_STYLE[status];
  return (
    <Badge variant={s.variant} className={cn("shrink-0", s.className)}>
      {s.label}
    </Badge>
  );
}

function SourceRow({ source }: { source: Source }) {
  const { sync, remove } = useSourceMutations();
  const [confirming, setConfirming] = React.useState(false);

  React.useEffect(() => {
    if (!confirming) return;
    const t = setTimeout(() => setConfirming(false), 4000);
    return () => clearTimeout(t);
  }, [confirming]);

  const busy = source.status === "syncing" || source.status === "pending";

  return (
    <li className="flex flex-col gap-1 rounded-md px-2 py-1.5 hover:bg-accent/50">
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate text-sm" title={source.title}>
          <span className="text-muted-foreground">{source.kind}</span> · {source.title}
        </span>
        <StatusBadge status={source.status} />
      </div>
      <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
        <span className="flex-1">
          {source.chunk_count} chunks · {source.document_count} docs
          {source.last_synced_at ? ` · ${timeAgo(source.last_synced_at)}` : ""}
        </span>
        <Button
          variant="ghost"
          size="sm"
          className="h-6 px-2 text-[11px]"
          disabled={sync.isPending || busy}
          onClick={() => sync.mutate(source.id)}
          title="Re-sync + re-embed"
        >
          {sync.isPending ? <Spinner className="h-3 w-3" /> : "Sync"}
        </Button>
        <Button
          variant="ghost"
          size="icon"
          className="h-6 w-6 text-destructive"
          aria-label={`Delete ${source.title}`}
          disabled={remove.isPending}
          onClick={() => (confirming ? remove.mutate(source.id) : setConfirming(true))}
          title={confirming ? "Click again to confirm" : "Delete"}
        >
          {remove.isPending ? <Spinner className="h-3 w-3" /> : confirming ? "?" : "✕"}
        </Button>
      </div>
      {source.status === "error" && source.last_error ? (
        <p className="truncate text-[11px] text-destructive" title={source.last_error}>
          {source.last_error}
        </p>
      ) : null}
    </li>
  );
}
