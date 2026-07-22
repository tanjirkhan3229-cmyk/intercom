/**
 * Shared domain vocabulary — mirrors the RFC-002 data layer so the web app, widget,
 * and SDK agree on the same string unions. Keep in sync with the API's Postgres enums
 * / CHECK constraints.
 */

/** Membership roles (RFC-002 §5, RFC-001 §10). */
export type Role = "owner" | "admin" | "agent" | "restricted";
export const ROLES: readonly Role[] = ["owner", "admin", "agent", "restricted"] as const;

/** Conversation state machine (Postgres enum `conversation_state`). */
export type ConversationState = "open" | "snoozed" | "closed";

/** Channels a conversation can arrive on (RFC-002 §5.3). */
export type ChannelType =
  | "chat"
  | "email"
  | "whatsapp"
  | "messenger_fb"
  | "instagram"
  | "sms"
  | "voice"
  | "api";

/** Who authored a conversation part. */
export type AuthorKind = "contact" | "admin" | "ai_agent" | "system";

/** Conversation part kinds present in phase 0 (RFC-002 §5.3). */
export type PartType = "comment" | "note" | "assignment" | "state_change" | "rating";

/** Prefixed base62 public identifier, e.g. `wrk_...`, `cnv_...` (RFC-002 §5.1). */
export type PublicId = string;

/** Keyset-paginated envelope — no OFFSET anywhere (RFC-002 §6). */
export interface Page<T> {
  data: T[];
  next_cursor: string | null;
}

/** Standard API error envelope (see relay.core.errors). */
export interface ApiError {
  error: {
    code: string;
    message: string;
    request_id?: string;
    details?: Record<string, unknown>;
  };
}
