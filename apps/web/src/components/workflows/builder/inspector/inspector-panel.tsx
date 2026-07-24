"use client";

import { Button } from "@/components/ui/button";
import { Label, Select } from "@/components/ui/primitives";
import {
  FRAMEWORK_TRIGGER_KEYS,
  OUTSIDE_OFFICE_HOURS_PREDICATE,
  SOURCED_TRIGGER_KEYS,
  type ConditionNode,
  type GraphError,
  type Predicate,
  type TriggerKey,
  type TriggerNode,
  type WaitNode,
  type WorkflowNode,
} from "@/lib/workflows/contract";
import { PredicateEditor } from "../../predicate/predicate-editor";
import { sourceHandles } from "../node-defs";
import { ActionConfig } from "./action-config";
import { BotStepConfig } from "./bot-step-config";
import { DurationField } from "./duration-field";

export interface NodeChoice {
  id: string;
  label: string;
}

const AIDE_UNRESOLVED_PREDICATE: Predicate = {
  op: "ne",
  field: "conversation.ai_status",
  value: "resolved",
};

export function InspectorPanel({
  node,
  errors,
  nodeChoices,
  edgeTargets,
  onChange,
  onDelete,
  onConnect,
}: {
  node: WorkflowNode | null;
  errors: GraphError[];
  /** All nodes eligible to be a target (non-trigger, excluding the selected node). */
  nodeChoices: NodeChoice[];
  /** Current target node id per source-handle id of the selected node. */
  edgeTargets: Record<string, string | undefined>;
  onChange: (next: WorkflowNode) => void;
  onDelete: (id: string) => void;
  onConnect: (handle: string, targetId: string) => void;
}) {
  if (!node) {
    return (
      <div className="flex h-full items-center justify-center p-6 text-center text-sm text-muted-foreground">
        Select a node to configure it, or drag one in from the palette.
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col" data-testid="inspector" data-selected-node-id={node.id}>
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <h3 className="text-sm font-semibold capitalize">{node.type.replace("_", " ")}</h3>
        {node.type !== "trigger" && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            data-testid="inspector-delete"
            onClick={() => onDelete(node.id)}
          >
            Delete
          </Button>
        )}
      </div>

      <div className="flex-1 overflow-y-auto p-4">
        {node.type === "trigger" && <TriggerConfig node={node} onChange={onChange} />}
        {node.type === "condition" && <ConditionConfig node={node} onChange={onChange} />}
        {node.type === "action" && <ActionConfig node={node} onChange={onChange} />}
        {node.type === "bot_step" && <BotStepConfig node={node} onChange={onChange} />}
        {node.type === "wait" && <WaitConfig node={node} onChange={onChange} />}
        {node.type === "end" && (
          <p className="text-xs text-muted-foreground">
            Terminal node — the run finishes here. No configuration.
          </p>
        )}

        <ConnectionsSection
          node={node}
          nodeChoices={nodeChoices}
          edgeTargets={edgeTargets}
          onConnect={onConnect}
        />

        {errors.length > 0 && (
          <ul className="mt-4 space-y-1 rounded-md border border-destructive/40 bg-destructive/5 p-2">
            {errors.map((e, i) => (
              <li key={i} className="text-xs text-destructive" data-testid="inspector-error">
                {e.message}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function TriggerConfig({
  node,
  onChange,
}: {
  node: TriggerNode;
  onChange: (next: TriggerNode) => void;
}) {
  const hasFilter = node.filter !== undefined && node.filter !== null;
  return (
    <div className="flex flex-col gap-3">
      <div>
        <Label>Run when</Label>
        <Select
          data-testid="trigger-key"
          value={node.trigger}
          onChange={(e) => onChange({ ...node, trigger: e.target.value as TriggerKey })}
        >
          <optgroup label="Live events">
            {SOURCED_TRIGGER_KEYS.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </optgroup>
          <optgroup label="Coming soon (authorable now)">
            {FRAMEWORK_TRIGGER_KEYS.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </optgroup>
        </Select>
      </div>
      <label className="flex items-center gap-2 text-xs text-muted-foreground">
        <input
          type="checkbox"
          data-testid="trigger-filter-toggle"
          checked={hasFilter}
          onChange={(e) =>
            onChange({ ...node, filter: e.target.checked ? { op: "and", clauses: [] } : null })
          }
        />
        Only run if the event matches a filter
      </label>
      {hasFilter && (
        <div className="rounded-md border border-border p-2">
          <PredicateEditor
            value={node.filter ?? undefined}
            onChange={(p) => onChange({ ...node, filter: p })}
          />
        </div>
      )}
    </div>
  );
}

function ConditionConfig({
  node,
  onChange,
}: {
  node: ConditionNode;
  onChange: (next: ConditionNode) => void;
}) {
  return (
    <div className="flex flex-col gap-3">
      <div>
        <Label>Quick presets</Label>
        <div className="flex flex-wrap gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            data-testid="preset-office-hours"
            onClick={() => onChange({ ...node, predicate: OUTSIDE_OFFICE_HOURS_PREDICATE })}
          >
            Outside office hours
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            data-testid="preset-aide-unresolved"
            onClick={() => onChange({ ...node, predicate: AIDE_UNRESOLVED_PREDICATE })}
          >
            Aide hasn&apos;t resolved
          </Button>
        </div>
      </div>
      <div>
        <Label>Condition</Label>
        <PredicateEditor
          value={node.predicate}
          onChange={(p) => onChange({ ...node, predicate: p })}
        />
      </div>
      <p className="text-[11px] text-muted-foreground">
        Wire the <strong>yes</strong> and <strong>no</strong> outputs on the node to the next steps.
      </p>
    </div>
  );
}

function WaitConfig({ node, onChange }: { node: WaitNode; onChange: (next: WaitNode) => void }) {
  return <DurationField value={node.params} onChange={(d) => onChange({ ...node, params: d })} />;
}

/**
 * Click/keyboard-friendly alternative to dragging edges on the canvas: pick each output's target
 * node from a dropdown. Drag on the canvas still works; this makes wiring accessible and testable.
 */
function ConnectionsSection({
  node,
  nodeChoices,
  edgeTargets,
  onConnect,
}: {
  node: WorkflowNode;
  nodeChoices: NodeChoice[];
  edgeTargets: Record<string, string | undefined>;
  onConnect: (handle: string, targetId: string) => void;
}) {
  const handles = sourceHandles(node);
  if (handles.length === 0) return null;
  const choices = nodeChoices.filter((c) => c.id !== node.id);

  return (
    <div className="mt-5 border-t border-border pt-4" data-testid="connections">
      <p className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        Connections
      </p>
      <div className="flex flex-col gap-2">
        {handles.map((h) => (
          <div key={h.id}>
            <Label>{h.label ? `${h.label} →` : "Next step →"}</Label>
            <Select
              data-testid={`connect-${h.id}`}
              value={edgeTargets[h.id] ?? ""}
              onChange={(e) => onConnect(h.id, e.target.value)}
            >
              <option value="">— none —</option>
              {choices.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.label}
                </option>
              ))}
            </Select>
          </div>
        ))}
      </div>
    </div>
  );
}
