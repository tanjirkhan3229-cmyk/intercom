"use client";

import { Handle, Position, type NodeProps, type NodeTypes } from "@xyflow/react";
import {
  Clock,
  Flag,
  GitBranch,
  MessageSquare,
  Sparkles,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { NodeType } from "@/lib/workflows/contract";
import type { WFNode } from "@/lib/workflows/mappers";
import { hasTargetHandle, nodeMeta, sourceHandles } from "./node-defs";
import { useBuilderContext } from "./context";

/** Per-type visual identity — icon + an accent strip and a tinted icon chip. Class strings are
 * literal so Tailwind's JIT keeps them. */
const TYPE_STYLES: Record<NodeType, { icon: LucideIcon; strip: string; chip: string }> = {
  trigger: { icon: Zap, strip: "bg-emerald-500", chip: "bg-emerald-500/10 text-emerald-600" },
  condition: { icon: GitBranch, strip: "bg-amber-500", chip: "bg-amber-500/10 text-amber-600" },
  action: { icon: Sparkles, strip: "bg-sky-500", chip: "bg-sky-500/10 text-sky-600" },
  bot_step: { icon: MessageSquare, strip: "bg-violet-500", chip: "bg-violet-500/10 text-violet-600" },
  wait: { icon: Clock, strip: "bg-orange-500", chip: "bg-orange-500/10 text-orange-600" },
  end: { icon: Flag, strip: "bg-slate-400", chip: "bg-slate-400/10 text-slate-500" },
};

const HANDLE_CLASS = "!h-2.5 !w-2.5 !border-2 !border-background !bg-muted-foreground/60";

/**
 * One presentational component for every node type — React Flow calls it per registered type key.
 * Renders a card with a colored accent strip, a tinted type icon, title + summary, an error ring +
 * badge when validation flags the node, and the correct target/source handles (ids must match the
 * mapper's edge slots).
 */
function WorkflowFlowNode({ id, data, selected }: NodeProps<WFNode>) {
  const node = data.node;
  const meta = nodeMeta(node);
  const style = TYPE_STYLES[node.type];
  const Icon = style.icon;
  const sources = sourceHandles(node);
  const { errorsByNode } = useBuilderContext();
  const errors = errorsByNode.get(id) ?? [];
  const hasError = errors.length > 0;

  return (
    <div
      data-testid="wf-node"
      data-node-type={node.type}
      data-node-id={id}
      className={cn(
        "group relative min-w-[200px] overflow-hidden rounded-xl border bg-card pl-4 pr-3 py-2.5 shadow-sm transition-all",
        "hover:shadow-md",
        selected ? "border-primary ring-2 ring-primary/40" : "border-border",
        hasError && "border-destructive ring-2 ring-destructive/40",
      )}
    >
      {/* accent strip */}
      <span className={cn("absolute inset-y-0 left-0 w-1.5", style.strip)} aria-hidden />

      {hasTargetHandle(node) && (
        <Handle type="target" position={Position.Left} id="in" className={HANDLE_CLASS} />
      )}

      <div className="flex items-center gap-2.5">
        <span
          className={cn(
            "flex h-7 w-7 shrink-0 items-center justify-center rounded-md",
            style.chip,
          )}
          aria-hidden
        >
          <Icon className="h-4 w-4" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-[11px] font-semibold uppercase tracking-wide text-foreground">
              {meta.title}
            </span>
            {hasError && (
              <span
                data-testid="wf-node-error"
                title={errors.map((e) => e.message).join("\n")}
                className="ml-auto inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-destructive px-1 text-[10px] font-bold text-destructive-foreground"
              >
                {errors.length}
              </span>
            )}
          </div>
          {meta.subtitle && (
            <p className="mt-0.5 max-w-[210px] truncate text-xs text-muted-foreground">
              {meta.subtitle}
            </p>
          )}
        </div>
      </div>

      {sources.map((h, i) => (
        <Handle
          key={h.id}
          type="source"
          position={Position.Right}
          id={h.id}
          className={HANDLE_CLASS}
          style={{ top: `${((i + 1) / (sources.length + 1)) * 100}%` }}
        >
          {h.label && (
            <span className="pointer-events-none absolute right-3 -translate-y-1/2 rounded bg-background/90 px-1 text-[9px] font-medium text-muted-foreground shadow-sm">
              {h.label}
            </span>
          )}
        </Handle>
      ))}
    </div>
  );
}

// Stable identity (React Flow requires nodeTypes not to change between renders). Every graph node
// type maps to the same component, which specializes on `data.node`.
export const NODE_TYPES: NodeTypes = {
  trigger: WorkflowFlowNode,
  condition: WorkflowFlowNode,
  action: WorkflowFlowNode,
  bot_step: WorkflowFlowNode,
  wait: WorkflowFlowNode,
  end: WorkflowFlowNode,
};
