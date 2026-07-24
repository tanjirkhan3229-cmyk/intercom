"use client";

import * as React from "react";
import Link from "next/link";
import type { Route } from "next";
import {
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Connection,
  type IsValidConnection,
} from "@xyflow/react";
import { RelayApiError } from "@/lib/api";
import type { GraphError, Workflow, WorkflowGraph, WorkflowNode } from "@/lib/workflows/contract";
import { edgeLabel, flowToGraph, graphToFlow, type WFEdge, type WFNode } from "@/lib/workflows/mappers";
import { validateGraph } from "@/lib/workflows/validate";
import { useSaveDraft, usePublish, useWorkflow } from "@/lib/workflows/workflows-hooks";
import { Badge } from "@/components/ui/primitives";
import { LoadingState, ErrorState } from "@/components/inbox/states";
import { BuilderProvider, groupErrorsByNode } from "./context";
import { createNode, newNodeId, nodeMeta, sourceHandles, type PaletteKind } from "./node-defs";
import { NodePalette } from "./node-palette";
import { PublishBar, type SaveState } from "./publish-bar";
import { ValidationPanel } from "./validation-panel";
import { WorkflowCanvas } from "./workflow-canvas";
import { InspectorPanel } from "./inspector/inspector-panel";

export function WorkflowBuilder({ workflowId }: { workflowId: string }) {
  const wf = useWorkflow(workflowId);

  if (wf.isLoading) return <LoadingState label="Loading workflow…" />;
  if (wf.isError) return <ErrorState error={wf.error} onRetry={() => void wf.refetch()} />;
  if (!wf.data) return null;

  const draftGraph: WorkflowGraph = wf.data.draft?.graph ?? { nodes: [] };
  return (
    <ReactFlowProvider>
      {/* Key by workflow id (not draft id): the canvas mounts once per workflow and must NOT remount
          on publish/autosave refetches — a remount would reset the editing session and drop an edit
          made in the publish→refetch window. */}
      <BuilderCanvas key={workflowId} workflow={wf.data} initialGraph={draftGraph} />
    </ReactFlowProvider>
  );
}

function BuilderCanvas({
  workflow,
  initialGraph,
}: {
  workflow: Workflow;
  initialGraph: WorkflowGraph;
}) {
  // Snapshot the initial graph → React Flow state exactly once at mount (a lazy initializer, so
  // autosave/publish refetches that change the `initialGraph` prop never clobber in-progress edits).
  const [initial] = React.useState(() => graphToFlow(initialGraph));
  const [nodes, setNodes, onNodesChange] = useNodesState<WFNode>(initial.nodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState<WFEdge>(initial.edges);
  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const [serverErrors, setServerErrors] = React.useState<GraphError[]>([]);
  const rf = useReactFlow<WFNode, WFEdge>();

  const saveDraft = useSaveDraft(workflow.id);
  const publish = usePublish(workflow.id);

  const graph = React.useMemo(() => flowToGraph(nodes, edges), [nodes, edges]);
  // Structural signature with layout normalized out — validation depends on structure, never on
  // node positions, so this lets a drag reposition nodes without re-running full graph validation.
  const structuralKey = React.useMemo(
    () => JSON.stringify(graph.nodes.map((n) => ({ ...n, ui: null }))),
    [graph],
  );
  const clientErrors = React.useMemo(
    () => validateGraph(graph),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- re-validate on structural change only
    [structuralKey],
  );
  const allErrors = React.useMemo(
    () => [...clientErrors, ...serverErrors],
    [clientErrors, serverErrors],
  );
  const errorsByNode = React.useMemo(() => groupErrorsByNode(allErrors), [allErrors]);
  // Publish is gated on CLIENT errors only — a stale server error must not block a retry (the user
  // clears it by editing or re-publishing). Server errors are shown, not gating.
  const clientErrorItems = clientErrors.filter((e) => e.severity === "error");
  const warnItems = allErrors.filter((e) => e.severity === "warning");

  // Debounced autosave, triggered only on a *structural* graph change. Node selection/hover produce
  // a new `graph` object that is structurally identical (flowToGraph ignores selection), so we
  // compare serialized graphs to avoid a PUT on every click. The first computed graph is the loaded
  // baseline and is never saved.
  const graphJson = React.useMemo(() => JSON.stringify(graph), [graph]);
  const savedJsonRef = React.useRef<string | null>(null);
  const saveRef = React.useRef(saveDraft);
  const [dirty, setDirty] = React.useState(false);
  React.useEffect(() => {
    saveRef.current = saveDraft;
  });
  React.useEffect(() => {
    if (savedJsonRef.current === null) {
      savedJsonRef.current = graphJson; // baseline = loaded draft
      return;
    }
    if (graphJson === savedJsonRef.current) return; // no real change (selection/measure churn)
    setDirty(true);
    const snapshot = graphJson;
    const t = setTimeout(() => {
      saveRef.current.mutate(graph, {
        onSuccess: () => {
          savedJsonRef.current = snapshot;
          setDirty(false);
        },
      });
    }, 800);
    return () => clearTimeout(t);
  }, [graphJson, graph]);

  const hasTrigger = nodes.some((n) => n.data.node.type === "trigger");

  const addNode = React.useCallback(
    (kind: PaletteKind) => {
      const id = newNodeId();
      const node = createNode(kind, id);
      setNodes((ns) => [
        ...ns,
        {
          id,
          type: node.type,
          position: { x: 160 + (ns.length % 4) * 60, y: 120 + ns.length * 40 },
          data: { node },
        },
      ]);
      setSelectedId(id);
    },
    [setNodes],
  );

  const isValidConnection = React.useCallback<IsValidConnection<WFEdge>>(
    (conn) => {
      if (!conn.source || !conn.target || conn.source === conn.target) return false;
      const target = nodes.find((n) => n.id === conn.target);
      // No edge may terminate at the trigger (it is the entry).
      return !!target && target.data.node.type !== "trigger";
    },
    [nodes],
  );

  const onConnect = React.useCallback(
    (conn: Connection) => {
      if (!conn.source || !conn.target) return;
      const handle = conn.sourceHandle ?? "next";
      const edge: WFEdge = {
        id: `e:${conn.source}:${handle}`,
        source: conn.source,
        sourceHandle: handle,
        target: conn.target,
        targetHandle: conn.targetHandle ?? "in",
        label: edgeLabel(handle),
      };
      // A source handle holds at most one edge — reconnecting replaces it (matches graph.py's
      // single-target edge fields).
      setEdges((es) => [
        ...es.filter((e) => !(e.source === conn.source && (e.sourceHandle ?? "next") === handle)),
        edge,
      ]);
    },
    [setEdges],
  );

  const updateNode = React.useCallback(
    (next: WorkflowNode) => {
      setNodes((ns) => ns.map((n) => (n.id === next.id ? { ...n, data: { node: next } } : n)));
      // Prune edges whose source handle no longer exists (e.g. a removed bot option).
      const valid = new Set(sourceHandles(next).map((h) => h.id));
      setEdges((es) =>
        es.filter((e) => e.source !== next.id || valid.has(e.sourceHandle ?? "next")),
      );
      setServerErrors([]);
    },
    [setNodes, setEdges],
  );

  const deleteNode = React.useCallback(
    (id: string) => {
      setNodes((ns) => ns.filter((n) => n.id !== id));
      setEdges((es) => es.filter((e) => e.source !== id && e.target !== id));
      setSelectedId((cur) => (cur === id ? null : cur));
    },
    [setNodes, setEdges],
  );

  const connectHandle = React.useCallback(
    (source: string, handle: string, target: string) => {
      setEdges((es) => {
        const rest = es.filter(
          (e) => !(e.source === source && (e.sourceHandle ?? "next") === handle),
        );
        if (!target) return rest;
        return [
          ...rest,
          {
            id: `e:${source}:${handle}`,
            source,
            sourceHandle: handle,
            target,
            targetHandle: "in",
            label: edgeLabel(handle),
          },
        ];
      });
    },
    [setEdges],
  );

  const focusNode = React.useCallback(
    (id: string) => {
      const n = nodes.find((x) => x.id === id);
      if (n) {
        void rf.setCenter(n.position.x + 100, n.position.y + 40, { zoom: 1.15, duration: 300 });
        setSelectedId(id);
      }
    },
    [nodes, rf],
  );

  const onPublish = React.useCallback(async () => {
    setServerErrors([]);
    if (clientErrorItems.length > 0) return;
    try {
      await publish.mutateAsync(graph);
    } catch (err) {
      const message =
        err instanceof RelayApiError ? err.message : "Publish failed. Please try again.";
      setServerErrors([{ code: "server", message, severity: "error" }]);
    }
  }, [clientErrorItems.length, graph, publish]);

  const saveState: SaveState = saveDraft.isPending
    ? "saving"
    : dirty
      ? "unsaved"
      : saveDraft.isError
        ? "error"
        : saveDraft.isSuccess
          ? "saved"
          : "idle";

  const selectedNode = selectedId
    ? (nodes.find((n) => n.id === selectedId)?.data.node ?? null)
    : null;
  const selectedErrors = selectedId ? (errorsByNode.get(selectedId) ?? []) : [];

  const nodeChoices = React.useMemo(
    () =>
      nodes
        .filter((n) => n.data.node.type !== "trigger")
        .map((n) => ({ id: n.id, label: `${nodeMeta(n.data.node).title} (${n.id.slice(0, 6)})` })),
    [nodes],
  );
  const edgeTargets = React.useMemo(() => {
    const map: Record<string, string> = {};
    if (!selectedId) return map;
    for (const e of edges) {
      if (e.source === selectedId) map[e.sourceHandle ?? "next"] = e.target;
    }
    return map;
  }, [edges, selectedId]);

  return (
    <div className="flex h-screen flex-col bg-background">
      <header className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
        <div className="flex items-center gap-3">
          <Link
            href={"/app/workflows" as Route}
            className="text-xs font-medium text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
          >
            ← Workflows
          </Link>
          <h1 className="text-sm font-semibold" data-testid="workflow-name">
            {workflow.name}
          </h1>
          <Badge
            variant={workflow.status === "active" ? "default" : "muted"}
            className="capitalize"
            data-testid="workflow-status"
          >
            {workflow.status}
          </Badge>
          {workflow.active_version != null && (
            <Badge variant="muted" data-testid="published-version">
              v{workflow.active_version} live
            </Badge>
          )}
        </div>
        <Link
          href={`/app/workflows/${workflow.id}/runs` as Route}
          className="text-xs font-medium text-primary underline-offset-4 hover:underline"
          data-testid="view-runs"
        >
          View runs →
        </Link>
      </header>

      <PublishBar
        saveState={saveState}
        errorCount={clientErrorItems.length}
        warningCount={warnItems.length}
        publishing={publish.isPending}
        onPublish={() => void onPublish()}
      />

      <div className="flex min-h-0 flex-1">
        <NodePalette onAdd={addNode} hasTrigger={hasTrigger} />
        <div className="relative min-w-0 flex-1">
          <BuilderProvider errorsByNode={errorsByNode}>
            <WorkflowCanvas
              nodes={nodes}
              edges={edges}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onConnect={onConnect}
              isValidConnection={isValidConnection}
              onSelect={setSelectedId}
            />
          </BuilderProvider>
          {nodes.length === 0 && (
            <div className="pointer-events-none absolute inset-0 flex items-center justify-center p-6">
              <p className="rounded-xl border border-dashed border-border bg-card/80 px-5 py-4 text-center text-sm text-muted-foreground shadow-sm backdrop-blur-sm">
                Add a <span className="font-semibold text-foreground">Trigger</span> from the left
                to start building your workflow.
              </p>
            </div>
          )}
        </div>
        <aside className="w-80 shrink-0 border-l border-border">
          <InspectorPanel
            node={selectedNode}
            errors={selectedErrors}
            nodeChoices={nodeChoices}
            edgeTargets={edgeTargets}
            onChange={updateNode}
            onDelete={deleteNode}
            onConnect={(handle, targetId) =>
              selectedNode && connectHandle(selectedNode.id, handle, targetId)
            }
          />
        </aside>
      </div>

      <ValidationPanel errors={allErrors} onFocusNode={focusNode} />
    </div>
  );
}
