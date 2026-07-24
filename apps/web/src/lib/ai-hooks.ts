"use client";

/**
 * TanStack Query data layer for the Neko AI agent surface (P1.3), matching the Help Center hook
 * style (`lib/hc-hooks.ts`): a settings query + explicit-save mutation, a usage/spend query, and a
 * preview mutation (the sandbox posts a message and renders the returned retrieval trace).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi, useAuth } from "./auth";
import { qk } from "./keys";
import type { AiSettings, AiSettingsInput, RunSearchParams, SandboxTurnInput } from "./types";

export function useAiSettings() {
  const api = useApi();
  const { status } = useAuth();
  return useQuery({
    queryKey: qk.aiSettings,
    queryFn: () => api.getAiSettings(),
    enabled: status === "authenticated",
  });
}

export function useUpdateAiSettings() {
  const api = useApi();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: AiSettingsInput) => api.updateAiSettings(input),
    onSuccess: (settings: AiSettings) => {
      qc.setQueryData(qk.aiSettings, settings);
      // The spend cap changed the routing math — refresh the usage/cap card.
      void qc.invalidateQueries({ queryKey: qk.nekoUsage });
    },
  });
}

export function useNekoUsage() {
  const api = useApi();
  const { status } = useAuth();
  return useQuery({
    queryKey: qk.nekoUsage,
    queryFn: () => api.getNekoUsage(),
    enabled: status === "authenticated",
  });
}

export function useNekoPreview() {
  const api = useApi();
  return useMutation({
    mutationFn: (input: SandboxTurnInput) => api.previewNeko(input),
  });
}

// --- Neko analytics (P1.4) ----------------------------------------------------

export function useNekoReport(range: { from?: string; to?: string }) {
  const api = useApi();
  const { status } = useAuth();
  return useQuery({
    queryKey: qk.nekoReport(range),
    queryFn: () => api.getNekoReport(range),
    enabled: status === "authenticated",
  });
}

export function useNekoCsat(range: { from?: string; to?: string }) {
  const api = useApi();
  const { status } = useAuth();
  return useQuery({
    queryKey: qk.nekoCsat(range),
    queryFn: () => api.getNekoCsat(range),
    enabled: status === "authenticated",
  });
}

export function useRunSearch(params: RunSearchParams) {
  const api = useApi();
  const { status } = useAuth();
  return useQuery({
    queryKey: qk.runs(params),
    queryFn: () => api.searchRuns(params),
    enabled: status === "authenticated",
  });
}

/** A single run's full trace — enabled only when a run is selected (the inspector detail). */
export function useRun(id: string | null) {
  const api = useApi();
  const { status } = useAuth();
  return useQuery({
    queryKey: qk.run(id ?? ""),
    queryFn: () => api.getRun(id as string),
    enabled: status === "authenticated" && !!id,
  });
}
