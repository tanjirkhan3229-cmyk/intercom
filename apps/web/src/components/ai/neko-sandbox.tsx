"use client";

import * as React from "react";
import { useNekoPreview } from "@/lib/ai-hooks";
import { Button } from "@/components/ui/button";
import { Textarea, Badge, Spinner } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import type { SandboxTurn } from "@/lib/types";

/**
 * Preview sandbox (RFC-003 §5): test a message against the workspace's current knowledge and see
 * exactly *why* an answer happened — the answer plus the retrieval trace (retrieved chunks + fused
 * scores, grounding score, citations). Persists nothing; the trace matches what a real turn writes
 * to `agent_runs`.
 */
export function NekoSandbox() {
  const preview = useNekoPreview();
  const [message, setMessage] = React.useState("");

  const run = () => {
    if (message.trim()) preview.mutate({ message: message.trim() });
  };

  const result = preview.data;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-2">
        <Textarea
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
              e.preventDefault();
              run();
            }
          }}
          placeholder="Ask Neko a question the way a customer would…"
          rows={3}
        />
        <div className="flex items-center gap-3">
          <Button size="sm" onClick={run} disabled={preview.isPending || !message.trim()}>
            {preview.isPending ? <Spinner className="h-3.5 w-3.5" /> : "Run preview"}
          </Button>
          <span className="text-xs text-muted-foreground">⌘/Ctrl + Enter</span>
          {preview.isError && <span className="text-xs text-destructive">Preview failed</span>}
        </div>
      </div>

      {result && <SandboxResult result={result} />}
    </div>
  );
}

function SandboxResult({ result }: { result: SandboxTurn }) {
  const cited = new Set(result.citations);
  return (
    <div className="flex flex-col gap-4 rounded-lg border border-border p-4">
      <div className="flex flex-wrap items-center gap-2">
        <OutcomeBadge outcome={result.outcome} reason={result.handoff_reason} />
        {result.grounding_score != null && (
          <Badge variant="muted">grounding {result.grounding_score.toFixed(2)}</Badge>
        )}
        {result.provider && <Badge variant="muted">{result.provider}</Badge>}
      </div>

      {result.answer && (
        <div>
          <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            Answer
          </p>
          <p className="mt-1 whitespace-pre-wrap text-sm">{result.answer}</p>
        </div>
      )}

      {result.rewritten_query && (
        <p className="text-xs text-muted-foreground">
          Search query: <span className="font-mono">{result.rewritten_query}</span>
        </p>
      )}

      <div>
        <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          Retrieval trace ({result.retrieved.length})
        </p>
        {result.retrieved.length === 0 ? (
          <p className="mt-1 text-sm text-muted-foreground">Nothing retrieved for this query.</p>
        ) : (
          <div className="mt-2 overflow-x-auto">
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
                {result.retrieved.map((c, i) => (
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
        )}
      </div>
    </div>
  );
}

function OutcomeBadge({ outcome, reason }: { outcome: string; reason: string | null }) {
  const tone =
    outcome === "answered"
      ? "bg-primary/10 text-primary"
      : outcome === "handoff"
        ? "bg-amber-500/10 text-amber-600 dark:text-amber-400"
        : "bg-muted text-muted-foreground";
  return (
    <span className={cn("inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium", tone)}>
      {outcome}
      {reason && ` · ${reason}`}
    </span>
  );
}
