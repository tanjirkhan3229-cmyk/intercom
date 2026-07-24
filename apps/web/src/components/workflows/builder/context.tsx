"use client";

import * as React from "react";
import type { GraphError } from "@/lib/workflows/contract";

/** Shared builder state that React Flow node components need but can't receive as props. */
interface BuilderContextValue {
  errorsByNode: Map<string, GraphError[]>;
}

const BuilderContext = React.createContext<BuilderContextValue>({ errorsByNode: new Map() });

export function BuilderProvider({
  errorsByNode,
  children,
}: {
  errorsByNode: Map<string, GraphError[]>;
  children: React.ReactNode;
}) {
  const value = React.useMemo(() => ({ errorsByNode }), [errorsByNode]);
  return <BuilderContext.Provider value={value}>{children}</BuilderContext.Provider>;
}

export function useBuilderContext(): BuilderContextValue {
  return React.useContext(BuilderContext);
}

/** Group graph errors by the node they belong to (errors without a nodeId are graph-level). */
export function groupErrorsByNode(errors: GraphError[]): Map<string, GraphError[]> {
  const map = new Map<string, GraphError[]>();
  for (const e of errors) {
    if (!e.nodeId) continue;
    const list = map.get(e.nodeId);
    if (list) list.push(e);
    else map.set(e.nodeId, [e]);
  }
  return map;
}
