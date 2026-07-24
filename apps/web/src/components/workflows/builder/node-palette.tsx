"use client";

import {
  Clock,
  Flag,
  GitBranch,
  ListChecks,
  MessageSquare,
  Sparkles,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { PALETTE, type PaletteKind } from "./node-defs";

const ICONS: Record<PaletteKind, LucideIcon> = {
  trigger: Zap,
  condition: GitBranch,
  collect: MessageSquare,
  ask: ListChecks,
  action: Sparkles,
  wait: Clock,
  end: Flag,
};

/** Click-to-add palette (keyboard-accessible; e2e drives it by testid). Trigger is disabled once the
 * graph already has one (exactly-one-trigger rule). */
export function NodePalette({
  onAdd,
  hasTrigger,
}: {
  onAdd: (kind: PaletteKind) => void;
  hasTrigger: boolean;
}) {
  return (
    <div className="flex w-52 shrink-0 flex-col gap-1.5 border-r border-border bg-muted/20 p-3" data-testid="palette">
      <p className="px-1 pb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        Add step
      </p>
      {PALETTE.map((item) => {
        const disabled = item.kind === "trigger" && hasTrigger;
        const Icon = ICONS[item.kind];
        return (
          <button
            key={item.kind}
            type="button"
            disabled={disabled}
            data-testid={`palette-${item.kind}`}
            onClick={() => onAdd(item.kind)}
            className="group flex items-center gap-2.5 rounded-lg border border-transparent bg-card/60 px-2.5 py-2 text-left transition-all hover:border-border hover:bg-card hover:shadow-sm disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:border-transparent disabled:hover:bg-card/60 disabled:hover:shadow-none"
          >
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground transition-colors group-hover:bg-primary/10 group-hover:text-primary">
              <Icon className="h-4 w-4" />
            </span>
            <span className="min-w-0">
              <span className="block text-sm font-medium leading-tight">{item.label}</span>
              <span className="block truncate text-[11px] text-muted-foreground">
                {item.description}
              </span>
            </span>
          </button>
        );
      })}
    </div>
  );
}
