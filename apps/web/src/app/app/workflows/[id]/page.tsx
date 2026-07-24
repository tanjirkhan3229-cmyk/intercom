"use client";

import dynamic from "next/dynamic";
import { useParams } from "next/navigation";
import { ErrorState, LoadingState } from "@/components/inbox/states";

// React Flow is client-only (measures the DOM on mount); load the builder with SSR disabled.
const WorkflowBuilder = dynamic(
  () => import("@/components/workflows/builder/workflow-builder").then((m) => m.WorkflowBuilder),
  { ssr: false, loading: () => <LoadingState label="Loading builder…" /> },
);

export default function WorkflowBuilderPage() {
  const params = useParams<{ id: string }>();
  const id = params?.id;
  if (!id) return <ErrorState title="Missing workflow id" />;
  return <WorkflowBuilder workflowId={id} />;
}
