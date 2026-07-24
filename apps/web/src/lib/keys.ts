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
  // Knowledge Hub sources (P1.1)
  sources: ["sources"] as const,
  // Neko AI agent (P1.3)
  aiSettings: ["ai-settings"] as const,
  nekoUsage: ["neko-usage"] as const,
  // Neko analytics (P1.4)
  nekoReport: (range: { from?: string; to?: string }) => ["neko-report", range] as const,
  nekoCsat: (range: { from?: string; to?: string }) => ["neko-csat", range] as const,
  runs: (params: Record<string, unknown>) => ["ai-runs", params] as const,
  run: (id: string) => ["ai-run", id] as const,
};
