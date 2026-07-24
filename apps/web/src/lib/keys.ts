import type { ListParams } from "./views";

/** Central query-key factory so invalidation from the realtime layer stays consistent. */
export const qk = {
  session: ["session"] as const,
  teams: ["teams"] as const,
  savedReplies: ["saved-replies"] as const,
  conversations: (params: ListParams) => ["conversations", params] as const,
  conversationsRoot: ["conversations"] as const,
  conversation: (id: string) => ["conversation", id] as const,
  parts: (id: string) => ["parts", id] as const,
  tags: (id: string) => ["tags", id] as const,
  contact: (id: string) => ["contact", id] as const,
  contactConversations: (id: string) => ["contact-conversations", id] as const,
  contactEvents: (id: string) => ["contact-events", id] as const,
  // Help Center (P0.8)
  collections: ["collections"] as const,
  articles: (params: { status?: string; collectionId?: string }) =>
    ["articles", params] as const,
  articlesRoot: ["articles"] as const,
  article: (id: string) => ["article", id] as const,
  helpCenter: ["help-center"] as const,
  // Workflows (P1.6). Distinct roots (not nested under ["workflow", id]) so invalidating the
  // workflow head does NOT prefix-match and refetch the versions/runs lists.
  workflowsRoot: ["workflows"] as const,
  workflow: (id: string) => ["workflow", id] as const,
  workflowVersions: (id: string) => ["workflow-versions", id] as const,
  workflowRuns: (id: string, filters: { status?: string; versionId?: string }) =>
    ["workflow-runs", id, filters] as const,
  workflowRun: (runId: string) => ["workflow-run", runId] as const,
  workflowRunSteps: (runId: string) => ["workflow-run", runId, "steps"] as const,
  attributeDefinitions: (entity: string) => ["attribute-definitions", entity] as const,
};
