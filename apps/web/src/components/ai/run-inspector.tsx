"use client";

import * as React from "react";
import { useRun, useRunSearch } from "@/lib/ai-hooks";
import { Input, Badge } from "@/components/ui/primitives";
import { LoadingState, ErrorState, EmptyState } from "@/components/inbox/states";
import { cn } from "@/lib/utils";
import type { AgentRunDetail, AgentRunSummary, TraceEvidence } from "@/lib/types";

/**
 * Run inspector (RFC-003 §8, P1.4): a searchable, filterable list of Neko turns → the full turn
 * trace (retrieval set, decisions, outputs). The goal is that a support engineer can answer "why
 * did Neko say X" without engineering help — so the detail shows the retrieved evidence *content*,
 * the grounding verdict, and the final answer, not just ids. Single-run loads are PK lookups (<1 s).
 */

const OUTCOMES = ["answered", "clarify", "handoff", "ineligible", "error"] as const;

export function RunInspector() {
  const [rawQuery, setRawQuery] = React.useState("");
  const [query, setQuery] = React.useState(""); // debounced
  const [outcome, setOutcome] = React.useState<string | undefined>(undefined);
  const [selected, setSelected] = React.useState<string | null>(null);

  React.useEffect(() => {
    const t = setTimeout(() => setQuery(rawQuery.trim()), 250);
    return () => clearTimeout(t);
  }, [rawQuery]);

  const list = useRunSearch({ q: query || undefined, outcome, limit: 50 });

  return (
    <div className="flex flex-col gap-4 lg:h-[calc(100vh-11rem)] lg:flex-row">
      {/* Search + list */}
      <div className="flex min-h-0 flex-col gap-3 lg:w-80 lg:shrink-0">
        <Input
          value={rawQuery}
          onChange={(e) => setRawQuery(e.target.value)}
          placeholder="Search the customer's question…"
        />
        <div className="flex flex-wrap gap-1">
          <FilterChip active={!outcome} onClick={() => setOutcome(undefined)}>
            all
          </FilterChip>
          {OUTCOMES.map((o) => (
            <FilterChip key={o} active={outcome === o} onClick={() => setOutcome(o)}>
              {o}
            </FilterChip>
          ))}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto rounded-lg border border-border">
          {list.isLoading ? (
            <LoadingState label="Loading runs…" className="h-40" />
          ) : list.isError ? (
            <ErrorState error={list.error} onRetry={() => void list.refetch()} className="h-40" />
          ) : list.data!.items.length === 0 ? (
            <EmptyState title="No runs match" hint="Adjust the search or outcome filter." className="h-40" />
          ) : (
            <ul className="divide-y divide-border">
              {list.data!.items.map((r) => (
                <RunRow
                  key={r.id}
                  run={r}
                  active={selected === r.id}
                  onClick={() => setSelected(r.id)}
                />
              ))}
              {list.data!.next_cursor && (
                // ponytail: first page (50) + a refine hint; add cursor "load more" if inspectors
                // routinely page past 50 within one workspace-window.
                <li className="px-3 py-2 text-center text-xs text-muted-foreground">
                  More runs — refine the search to narrow.
                </li>
              )}
            </ul>
          )}
        </div>
      </div>

      {/* Detail */}
      <div className="min-h-0 flex-1 overflow-y-auto">
        {selected ? (
          <RunDetail id={selected} />
        ) : (
          <EmptyState
            title="Select a run"
            hint="Pick a turn to see its retrieval set, decisions and answer."
            className="h-40"
          />
        )}
      </div>
    </div>
  );
}

function FilterChip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        "rounded-md px-2 py-0.5 text-xs font-medium capitalize transition-colors",
        active ? "bg-accent text-accent-foreground" : "text-muted-foreground hover:bg-accent/50",
      )}
    >
      {children}
    </button>
  );
}

function RunRow({
  run,
  active,
  onClick,
}: {
  run: AgentRunSummary;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <li>
      <button
        type="button"
        onClick={onClick}
        className={cn(
          "flex w-full flex-col gap-1 px-3 py-2 text-left transition-colors hover:bg-accent/40",
          active && "bg-accent/60",
        )}
      >
        <div className="flex items-center justify-between gap-2">
          <OutcomeBadge outcome={run.outcome} reason={run.handoff_reason} />
          <span className="shrink-0 text-[11px] tabular-nums text-muted-foreground">
            {new Date(run.created_at).toLocaleString()}
          </span>
        </div>
        <p className="line-clamp-2 text-xs">{run.query}</p>
        <div className="flex gap-3 text-[11px] tabular-nums text-muted-foreground">
          {run.latency_total_ms != null && <span>{Math.round(run.latency_total_ms)} ms</span>}
          <span>${run.cost_usd.toFixed(4)}</span>
          {run.grounding_score != null && <span>grounding {run.grounding_score.toFixed(2)}</span>}
        </div>
      </button>
    </li>
  );
}

function RunDetail({ id }: { id: string }) {
  const run = useRun(id);
  if (run.isLoading) return <LoadingState label="Loading run…" className="h-40" />;
  if (run.isError)
    return <ErrorState error={run.error} onRetry={() => void run.refetch()} className="h-40" />;
  const r = run.data!;
  return (
    <div className="flex flex-col gap-4 rounded-lg border border-border p-4">
      <div className="flex flex-wrap items-center gap-2">
        <OutcomeBadge outcome={r.outcome} reason={r.handoff_reason} />
        {r.grounding_score != null && (
          <Badge variant="muted">grounding {r.grounding_score.toFixed(2)}</Badge>
        )}
        {r.provider && <Badge variant="muted">{r.provider}</Badge>}
        {r.language && <Badge variant="muted">{r.language}</Badge>}
        {r.safety_class && <Badge variant="muted">{r.safety_class}</Badge>}
      </div>

      <Field label="Customer asked">
        <p className="whitespace-pre-wrap text-sm">{r.query}</p>
      </Field>
      {r.rewritten_query && (
        <p className="text-xs text-muted-foreground">
          Search query: <span className="font-mono">{r.rewritten_query}</span>
        </p>
      )}
      {r.answer && (
        <Field label="Neko answered">
          <p className="whitespace-pre-wrap text-sm">{r.answer}</p>
        </Field>
      )}

      <Verdict verdict={r.verdict} />
      <RetrievalTrace run={r} />
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        {label}
      </p>
      <div className="mt-1">{children}</div>
    </div>
  );
}

function Verdict({ verdict }: { verdict: Record<string, unknown> }) {
  const grounded = verdict.grounded as boolean | undefined;
  const unsupported = (verdict.unsupported_claims as string[] | undefined) ?? [];
  const policy = (verdict.policy_flags as string[] | undefined) ?? [];
  if (grounded === undefined && unsupported.length === 0 && policy.length === 0) return null;
  return (
    <Field label="Verifier decision">
      <div className="flex flex-col gap-1 text-xs">
        {grounded !== undefined && (
          <span>
            grounded:{" "}
            <span className={grounded ? "text-primary" : "text-destructive"}>{String(grounded)}</span>
          </span>
        )}
        {unsupported.length > 0 && (
          <span className="text-destructive">unsupported claims: {unsupported.join("; ")}</span>
        )}
        {policy.length > 0 && (
          <span className="text-amber-600 dark:text-amber-400">
            policy flags: {policy.join(", ")}
          </span>
        )}
      </div>
    </Field>
  );
}

/**
 * The retrieval set with evidence *content* (the "why did Neko say X" payload). Prefer the trace's
 * rich evidence (title/heading/content); fall back to the id+score `retrieved` list if absent.
 */
function RetrievalTrace({ run }: { run: AgentRunDetail }) {
  const cited = new Set(run.citations);
  const evidence = (run.trace?.evidence as TraceEvidence[] | undefined) ?? [];

  if (evidence.length === 0 && run.retrieved.length === 0)
    return (
      <Field label="Retrieval trace">
        <p className="text-sm text-muted-foreground">Nothing retrieved for this query.</p>
      </Field>
    );

  if (evidence.length === 0)
    return (
      <Field label={`Retrieval trace (${run.retrieved.length})`}>
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="text-left text-muted-foreground">
              <tr>
                <th className="py-1 pr-3 font-medium">#</th>
                <th className="py-1 pr-3 font-medium">Source</th>
                <th className="py-1 pr-3 font-medium">Chunk</th>
                <th className="py-1 pr-3 font-medium">Score</th>
                <th className="py-1 font-medium">Cited</th>
              </tr>
            </thead>
            <tbody>
              {run.retrieved.map((c, i) => (
                <tr
                  key={c.chunk_id}
                  className={cn("border-t border-border", cited.has(c.chunk_id) && "bg-primary/5")}
                >
                  <td className="py-1 pr-3 font-mono">{c.label ?? `c${i + 1}`}</td>
                  <td className="py-1 pr-3 capitalize">{c.source_kind}</td>
                  <td className="py-1 pr-3 font-mono text-muted-foreground">
                    {c.chunk_id.slice(0, 8)}…
                  </td>
                  <td className="py-1 pr-3 font-mono">{c.score.toFixed(4)}</td>
                  <td className="py-1">{cited.has(c.chunk_id) ? "✓" : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Field>
    );

  return (
    <Field label={`Retrieval trace (${evidence.length})`}>
      <div className="flex flex-col gap-2">
        {evidence.map((e, i) => {
          const isCited = cited.has(e.chunk_id);
          return (
            <div
              key={e.chunk_id}
              className={cn(
                "rounded-md border border-border p-3",
                isCited && "border-primary/40 bg-primary/5",
              )}
            >
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <span className="font-mono text-muted-foreground">{e.label ?? `c${i + 1}`}</span>
                {e.source_kind && <Badge variant="muted">{e.source_kind}</Badge>}
                {e.title && <span className="font-medium">{e.title}</span>}
                {e.heading_path && (
                  <span className="text-muted-foreground">› {e.heading_path}</span>
                )}
                {typeof e.score === "number" && (
                  <span className="ml-auto font-mono">{e.score.toFixed(4)}</span>
                )}
                {isCited && <Badge>cited</Badge>}
              </div>
              {e.content && (
                <p className="mt-2 whitespace-pre-wrap text-xs text-muted-foreground">
                  {e.content}
                </p>
              )}
            </div>
          );
        })}
      </div>
    </Field>
  );
}

function OutcomeBadge({ outcome, reason }: { outcome: string | null; reason: string | null }) {
  const tone =
    outcome === "answered"
      ? "bg-primary/10 text-primary"
      : outcome === "handoff"
        ? "bg-amber-500/10 text-amber-600 dark:text-amber-400"
        : "bg-muted text-muted-foreground";
  return (
    <span
      className={cn("inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium", tone)}
    >
      {outcome ?? "pending"}
      {reason && ` · ${reason}`}
    </span>
  );
}
