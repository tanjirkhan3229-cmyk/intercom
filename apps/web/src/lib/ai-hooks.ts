"use client";

/**
 * TanStack Query data layer for the Neko AI agent surface (P1.3), matching the Help Center hook
 * style (`lib/hc-hooks.ts`): a settings query + explicit-save mutation, a usage/spend query, and a
 * preview mutation (the sandbox posts a message and renders the returned retrieval trace).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi, useAuth } from "./auth";
import { qk } from "./keys";
import type { AiSettings, AiSettingsInput, SandboxTurnInput } from "./types";

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
