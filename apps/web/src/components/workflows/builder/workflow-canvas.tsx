"use client";

import "@xyflow/react/dist/style.css";
import {
  Background,
  BackgroundVariant,
  Controls,
  MarkerType,
  MiniMap,
  ReactFlow,
  type DefaultEdgeOptions,
  type IsValidConnection,
  type OnConnect,
  type OnEdgesChange,
  type OnNodesChange,
} from "@xyflow/react";
import type { NodeType } from "@/lib/workflows/contract";
import type { WFEdge, WFNode } from "@/lib/workflows/mappers";
import { NODE_TYPES } from "./workflow-node";

const MINIMAP_COLORS: Record<NodeType, string> = {
  trigger: "#10b981",
  condition: "#f59e0b",
  action: "#0ea5e9",
  bot_step: "#8b5cf6",
  wait: "#f97316",
  end: "#94a3b8",
};

const DEFAULT_EDGE_OPTIONS: DefaultEdgeOptions = {
  type: "smoothstep",
  markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16 },
  style: { strokeWidth: 1.5 },
};

/** Presentational React Flow host. All state + business rules live in the orchestrator. */
export function WorkflowCanvas({
  nodes,
  edges,
  onNodesChange,
  onEdgesChange,
  onConnect,
  isValidConnection,
  onSelect,
}: {
  nodes: WFNode[];
  edges: WFEdge[];
  onNodesChange: OnNodesChange<WFNode>;
  onEdgesChange: OnEdgesChange<WFEdge>;
  onConnect: OnConnect;
  isValidConnection: IsValidConnection<WFEdge>;
  onSelect: (id: string | null) => void;
}) {
  return (
    <div className="h-full w-full bg-muted/20" data-testid="workflow-canvas">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        defaultEdgeOptions={DEFAULT_EDGE_OPTIONS}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        isValidConnection={isValidConnection}
        onNodeClick={(_, node) => onSelect(node.id)}
        onPaneClick={() => onSelect(null)}
        fitView
        fitViewOptions={{ padding: 0.25 }}
        proOptions={{ hideAttribution: true }}
        deleteKeyCode={null}
      >
        <Background variant={BackgroundVariant.Dots} gap={22} size={1.4} className="!text-border" />
        <Controls className="!rounded-lg !border !border-border !shadow-sm" showInteractive={false} />
        <MiniMap
          pannable
          zoomable
          className="!rounded-lg !border !border-border"
          nodeColor={(n) => MINIMAP_COLORS[(n.type as NodeType) ?? "action"] ?? "#94a3b8"}
          nodeStrokeWidth={0}
          maskColor="hsl(var(--muted) / 0.6)"
        />
      </ReactFlow>
    </div>
  );
}
