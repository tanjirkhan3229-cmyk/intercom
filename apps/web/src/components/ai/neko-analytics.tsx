"use client";

import * as React from "react";
import { useNekoCsat, useNekoReport } from "@/lib/ai-hooks";
import { LoadingState, ErrorState, EmptyState } from "@/components/inbox/states";
import { cn } from "@/lib/utils";
import type { CsatBucket, NekoDailyPoint } from "@/lib/types";

/**
 * Neko analytics v0 (RFC-003 §8, P1.4): resolution & deflection over time, latency + cost, the
 * handoff-reasons breakdown, and the CSAT delta (Neko-touched vs not). Every figure comes from the
 * reporting-spine rollup (`/reports/neko`) — never a raw `agent_runs` scan. Charts are inline
 * SVG/CSS on the shared design tokens (no charting dependency), matching the sandbox trace UI.
 */

const RANGES = [
  { label: "7d", days: 7 },
  { label: "30d", days: 30 },
  { label: "90d", days: 90 },
] as const;

function rangeParams(days: number): { from: string; to: string } {
  const to = new Date();
  const from = new Date(to);
  from.setUTCDate(from.getUTCDate() - (days - 1));
  return { from: from.toISOString().slice(0, 10), to: to.toISOString().slice(0, 10) };
}

const pct = (x: number | null | undefined) => (x == null ? "—" : `${(x * 100).toFixed(0)}%`);
const usd = (x: number | null | undefined) => (x == null ? "—" : `$${x.toFixed(x < 1 ? 4 : 2)}`);
const ms = (x: number | null | undefined) => (x == null ? "—" : `${Math.round(x)} ms`);

export function NekoAnalytics() {
  const [days, setDays] = React.useState(30);
  const range = React.useMemo(() => rangeParams(days), [days]);
  const report = useNekoReport(range);
  const csat = useNekoCsat(range);

  if (report.isLoading) return <LoadingState label="Loading Neko analytics…" className="h-40" />;
  if (report.isError)
    return <ErrorState error={report.error} onRetry={() => void report.refetch()} className="h-40" />;

  const { points, totals } = report.data!;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center gap-1">
        {RANGES.map((r) => (
          <button
            key={r.label}
            type="button"
            onClick={() => setDays(r.days)}
            aria-pressed={days === r.days}
            className={cn(
              "rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
              days === r.days
                ? "bg-accent text-accent-foreground"
                : "text-muted-foreground hover:bg-accent/50",
            )}
          >
            Last {r.label}
          </button>
        ))}
      </div>

      {totals.conversations_engaged === 0 ? (
        <EmptyState
          title="No Neko activity in this window"
          hint="Once Neko handles conversations, resolution, deflection, cost and CSAT show here."
          className="h-40"
        />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
            <Stat label="Resolution rate" value={pct(totals.resolution_rate)} sub="of engaged" />
            <Stat label="Deflection rate" value={pct(totals.deflection_rate)} sub="no human" />
            <Stat label="Resolutions" value={String(totals.resolutions)} sub="billed, net" />
            <Stat
              label="Engaged"
              value={String(totals.conversations_engaged)}
              sub="conversations"
            />
            <Stat label="Cost / conv" value={usd(totals.avg_cost_per_conversation)} sub="avg" />
            <Stat label="Latency" value={ms(totals.avg_latency_ms)} sub="avg / turn" />
          </div>

          <Panel title="Resolution & deflection over time">
            <ResolutionChart points={points} />
          </Panel>

          <div className="grid gap-4 lg:grid-cols-2">
            <Panel title="Cost per day">
              <CostChart points={points} />
            </Panel>
            <Panel title="Handoff reasons">
              <HandoffBars reasons={totals.handoff_reasons} total={totals.conversations_handoff} />
            </Panel>
          </div>

          <Panel title="CSAT delta — Neko-touched vs everyone else">
            {csat.isLoading ? (
              <LoadingState label="Loading CSAT…" className="h-24" />
            ) : csat.isError ? (
              <ErrorState error={csat.error} onRetry={() => void csat.refetch()} className="h-24" />
            ) : (
              <CsatDelta
                neko={csat.data!.neko_touched}
                other={csat.data!.non_neko}
                delta={csat.data!.delta}
              />
            )}
          </Panel>
        </>
      )}
    </div>
  );
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-lg border border-border bg-muted/30 p-4">
      <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        {label}
      </p>
      <p className="mt-1 text-2xl font-semibold tabular-nums">{value}</p>
      {sub && <p className="text-xs text-muted-foreground">{sub}</p>}
    </div>
  );
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-border p-4">
      <h3 className="mb-3 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        {title}
      </h3>
      {children}
    </section>
  );
}

/**
 * Per-day stacked columns: column height ∝ conversations engaged that day, filled by answered
 * (primary) + handed-off (amber); the remaining muted space is engaged-but-neither (e.g. clarify).
 */
function ResolutionChart({ points }: { points: NekoDailyPoint[] }) {
  const max = Math.max(1, ...points.map((p) => p.conversations_engaged));
  return (
    <div>
      <div className="flex h-32 items-end gap-px overflow-x-auto">
        {points.map((p) => {
          const h = (p.conversations_engaged / max) * 100;
          const ans = p.conversations_engaged ? (p.conversations_answered / max) * 100 : 0;
          const ho = p.conversations_engaged ? (p.conversations_handoff / max) * 100 : 0;
          return (
            <div
              key={p.day}
              className="flex min-w-[6px] flex-1 flex-col justify-end"
              title={`${p.day}\nengaged ${p.conversations_engaged} · answered ${p.conversations_answered} · handoff ${p.conversations_handoff} · resolutions ${p.resolutions}`}
            >
              <div className="relative w-full rounded-sm bg-muted" style={{ height: `${h}%` }}>
                <div
                  className="absolute bottom-0 w-full bg-primary"
                  style={{ height: `${(ans / Math.max(h, 0.001)) * 100}%` }}
                />
                <div
                  className="absolute w-full bg-amber-500/80"
                  style={{
                    bottom: `${(ans / Math.max(h, 0.001)) * 100}%`,
                    height: `${(ho / Math.max(h, 0.001)) * 100}%`,
                  }}
                />
              </div>
            </div>
          );
        })}
      </div>
      <Legend
        items={[
          { color: "bg-primary", label: "answered" },
          { color: "bg-amber-500/80", label: "handoff" },
          { color: "bg-muted", label: "other" },
        ]}
      />
    </div>
  );
}

function CostChart({ points }: { points: NekoDailyPoint[] }) {
  const max = Math.max(0.0001, ...points.map((p) => p.cost_usd));
  return (
    <div className="flex h-24 items-end gap-px overflow-x-auto">
      {points.map((p) => (
        <div
          key={p.day}
          className="flex min-w-[6px] flex-1 flex-col justify-end"
          title={`${p.day}\ncost ${usd(p.cost_usd)} · runs ${p.runs_total} · avg latency ${ms(p.avg_latency_ms)}`}
        >
          <div
            className="w-full rounded-sm bg-primary/70"
            style={{ height: `${(p.cost_usd / max) * 100}%` }}
          />
        </div>
      ))}
    </div>
  );
}

function HandoffBars({
  reasons,
  total,
}: {
  reasons: Record<string, number>;
  total: number;
}) {
  const rows = Object.entries(reasons).sort((a, b) => b[1] - a[1]);
  if (rows.length === 0)
    return <p className="text-sm text-muted-foreground">No handoffs in this window.</p>;
  const max = Math.max(1, ...rows.map(([, n]) => n));
  return (
    <div className="flex flex-col gap-2">
      {rows.map(([reason, n]) => (
        <div key={reason} className="flex items-center gap-2 text-xs">
          <span className="w-40 shrink-0 truncate font-mono text-muted-foreground" title={reason}>
            {reason}
          </span>
          <div className="h-3 flex-1 rounded-sm bg-muted">
            <div
              className="h-3 rounded-sm bg-amber-500/70"
              style={{ width: `${(n / max) * 100}%` }}
            />
          </div>
          <span className="w-8 shrink-0 text-right tabular-nums">{n}</span>
        </div>
      ))}
      <p className="mt-1 text-xs text-muted-foreground">{total} handed-off conversations</p>
    </div>
  );
}

function CsatDelta({
  neko,
  other,
  delta,
}: {
  neko: CsatBucket;
  other: CsatBucket;
  delta: number | null;
}) {
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-6">
        <Cohort label="Neko-touched" bucket={neko} />
        <Cohort label="Everyone else" bucket={other} />
        <div className="rounded-lg border border-border bg-muted/30 p-4">
          <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            Delta
          </p>
          <p
            className={cn(
              "mt-1 text-2xl font-semibold tabular-nums",
              delta == null
                ? "text-muted-foreground"
                : delta >= 0
                  ? "text-primary"
                  : "text-destructive",
            )}
          >
            {delta == null ? "—" : `${delta >= 0 ? "+" : ""}${delta.toFixed(2)}`}
          </p>
          <p className="text-xs text-muted-foreground">Neko − others</p>
        </div>
      </div>
    </div>
  );
}

function Cohort({ label, bucket }: { label: string; bucket: CsatBucket }) {
  const max = Math.max(1, ...Object.values(bucket.distribution));
  return (
    <div className="min-w-[180px]">
      <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        {label}
      </p>
      <p className="mt-1 text-2xl font-semibold tabular-nums">
        {bucket.average == null ? "—" : bucket.average.toFixed(2)}
        <span className="ml-1 text-xs font-normal text-muted-foreground">
          ({bucket.count} rated)
        </span>
      </p>
      <div className="mt-2 flex flex-col gap-1">
        {[5, 4, 3, 2, 1].map((star) => {
          const n = bucket.distribution[String(star)] ?? 0;
          return (
            <div key={star} className="flex items-center gap-2 text-xs">
              <span className="w-3 text-muted-foreground">{star}</span>
              <div className="h-2 flex-1 rounded-sm bg-muted">
                <div
                  className="h-2 rounded-sm bg-primary/70"
                  style={{ width: `${(n / max) * 100}%` }}
                />
              </div>
              <span className="w-6 text-right tabular-nums text-muted-foreground">{n}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function Legend({ items }: { items: { color: string; label: string }[] }) {
  return (
    <div className="mt-2 flex flex-wrap gap-3 text-xs text-muted-foreground">
      {items.map((it) => (
        <span key={it.label} className="inline-flex items-center gap-1.5">
          <span className={cn("h-2.5 w-2.5 rounded-sm", it.color)} />
          {it.label}
        </span>
      ))}
    </div>
  );
}
