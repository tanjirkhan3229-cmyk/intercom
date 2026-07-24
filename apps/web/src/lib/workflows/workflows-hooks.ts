"use client";

/**
 * TanStack Query data layer for the workflow builder (P1.6), matching the inbox/help-center hook
 * style (`lib/hooks.ts`, `lib/hc-hooks.ts`): keyset-paginated lists, mutations that invalidate the
 * affected keys, `enabled` gated on auth. The builder autosaves the draft via `useSaveDraft`;
 * `usePublish` freezes an immutable version server-side.
 */
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type InfiniteData,
} from "@tanstack/react-query";
import type { Page } from "@relay/shared";
import { useApi, useAuth } from "../auth";
import { qk } from "../keys";
import type {
  AttributeDefinition,
  Workflow,
  WorkflowGraph,
  WorkflowPatchInput,
  WorkflowRun,
  WorkflowSummary,
  WorkflowVersion,
} from "./contract";

const nextCursor = (last: Page<unknown>) => last.next_cursor ?? undefined;

// --- Workflows list -----------------------------------------------------------

export function useWorkflows(status?: string) {
  const api = useApi();
  const { status: authStatus } = useAuth();
  return useInfiniteQuery({
    queryKey: [...qk.workflowsRoot, { status }] as const,
    queryFn: ({ pageParam }) =>
      api.listWorkflows({ status, cursor: pageParam as string | undefined }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: nextCursor,
    enabled: authStatus === "authenticated",
  });
}

export function flattenWorkflows(
  data: InfiniteData<Page<WorkflowSummary>> | undefined,
): WorkflowSummary[] {
  return data ? data.pages.flatMap((p) => p.items) : [];
}

export function useWorkflow(id: string | null) {
  const api = useApi();
  return useQuery({
    queryKey: id ? qk.workflow(id) : ["workflow", "none"],
    queryFn: () => api.getWorkflow(id as string),
    enabled: !!id,
  });
}

export function useCreateWorkflow() {
  const api = useApi();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => api.createWorkflow({ name }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: qk.workflowsRoot }),
  });
}

export function usePatchWorkflow(id: string) {
  const api = useApi();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: WorkflowPatchInput) => api.patchWorkflow(id, input),
    onSuccess: (wf: Workflow) => {
      qc.setQueryData(qk.workflow(id), wf);
      void qc.invalidateQueries({ queryKey: qk.workflowsRoot });
    },
  });
}

export function useDeleteWorkflow() {
  const api = useApi();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.deleteWorkflow(id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: qk.workflowsRoot }),
  });
}

// --- Draft + publish ----------------------------------------------------------

/** Autosave the draft graph. No optimistic cache write — the canvas already holds the authoritative
 * editing state; we only refetch the workflow head on settle so `updated_at`/draft id stay fresh. */
export function useSaveDraft(id: string) {
  const api = useApi();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (graph: WorkflowGraph) => api.saveDraft(id, graph),
    onSettled: () => void qc.invalidateQueries({ queryKey: qk.workflow(id) }),
  });
}

/** Publish freezes a new immutable version. On a 422 the caller reads `RelayApiError` to map the
 * server's `details.path` onto the offending node. */
export function usePublish(id: string) {
  const api = useApi();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (graph: WorkflowGraph) => api.publishWorkflow(id, graph),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk.workflow(id) });
      void qc.invalidateQueries({ queryKey: qk.workflowVersions(id) });
      void qc.invalidateQueries({ queryKey: qk.workflowsRoot });
    },
  });
}

// --- Versions -----------------------------------------------------------------

export function useWorkflowVersions(id: string | null) {
  const api = useApi();
  return useInfiniteQuery({
    queryKey: id ? qk.workflowVersions(id) : ["workflow-versions", "none"],
    queryFn: ({ pageParam }) =>
      api.listWorkflowVersions(id as string, pageParam as string | undefined),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: nextCursor,
    enabled: !!id,
  });
}

export function flattenVersions(
  data: InfiniteData<Page<WorkflowVersion>> | undefined,
): WorkflowVersion[] {
  return data ? data.pages.flatMap((p) => p.items) : [];
}

// --- Runs ---------------------------------------------------------------------

export function useWorkflowRuns(
  id: string | null,
  filters: { status?: string; versionId?: string } = {},
) {
  const api = useApi();
  return useInfiniteQuery({
    queryKey: id ? qk.workflowRuns(id, filters) : ["workflow-runs", "none", filters],
    queryFn: ({ pageParam }) =>
      api.listWorkflowRuns(id as string, {
        status: filters.status,
        versionId: filters.versionId,
        cursor: pageParam as string | undefined,
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: nextCursor,
    enabled: !!id,
  });
}

export function flattenRuns(data: InfiniteData<Page<WorkflowRun>> | undefined): WorkflowRun[] {
  return data ? data.pages.flatMap((p) => p.items) : [];
}

export function useWorkflowRun(runId: string | null) {
  const api = useApi();
  return useQuery({
    queryKey: runId ? qk.workflowRun(runId) : ["workflow-run", "none"],
    queryFn: () => api.getWorkflowRun(runId as string),
    enabled: !!runId,
  });
}

export function useWorkflowRunSteps(runId: string | null) {
  const api = useApi();
  return useQuery({
    queryKey: runId ? qk.workflowRunSteps(runId) : ["workflow-run", "none", "steps"],
    queryFn: () => api.listWorkflowRunSteps(runId as string),
    enabled: !!runId,
  });
}

export function useRerunFromStep(runId: string) {
  const api = useApi();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (fromNodeId: string) => api.rerunFromStep(runId, fromNodeId),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk.workflowRun(runId) });
      void qc.invalidateQueries({ queryKey: qk.workflowRunSteps(runId) });
    },
  });
}

// --- Attribute definitions (predicate field picker) ---------------------------

export function useAttributeDefinitions(entity: "contact" | "company") {
  const api = useApi();
  const { status } = useAuth();
  return useQuery<AttributeDefinition[]>({
    queryKey: qk.attributeDefinitions(entity),
    queryFn: () => api.listAttributeDefinitions(entity),
    enabled: status === "authenticated",
    staleTime: 5 * 60_000,
  });
}
