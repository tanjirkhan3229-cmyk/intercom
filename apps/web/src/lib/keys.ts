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
};
