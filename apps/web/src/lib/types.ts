/**
 * API DTOs consumed by the agent app — hand-mirrored from the FastAPI response models
 * (RFC-002 §5.3/§5.4). These layer on top of the thin `@relay/sdk-ts` transport; once the
 * OpenAPI→TS generation runs in CI (`make sdk`), these can be swapped for the generated
 * models. String unions come from `@relay/shared` so the API, SDK, and web app agree.
 */
import type {
  AuthorKind,
  ChannelType,
  ConversationState,
  PartType,
  Role,
} from "@relay/shared";

export type { AuthorKind, ChannelType, ConversationState, PartType, Role };

export interface Conversation {
  id: string;
  contact_id: string;
  channel: ChannelType;
  state: ConversationState;
  assignee_id: string | null;
  team_id: string | null;
  priority: boolean;
  waiting_since: string | null;
  snoozed_until: string | null;
  last_part_at: string;
  first_contact_reply_at: string | null;
  ai_status: string | null;
  created_at: string;
}

export interface Attachment {
  key?: string;
  url?: string;
  name?: string;
  content_type?: string;
  size?: number;
  [k: string]: unknown;
}

export interface Part {
  id: string;
  conversation_id: string;
  author_kind: AuthorKind;
  author_id: string | null;
  part_type: PartType;
  body: string | null;
  attachments: Attachment[];
  meta: Record<string, unknown>;
  created_at: string;
}

export interface Contact {
  id: string;
  kind: string;
  external_id: string | null;
  email: string | null;
  phone: string | null;
  name: string | null;
  custom: Record<string, unknown>;
  last_seen_at: string | null;
  created_at: string;
}

export interface ContactEvent {
  name: string;
  contact_id: string;
  properties: Record<string, unknown>;
  created_at: string;
}

export interface SavedReply {
  id: string;
  shortcut: string;
  title: string;
  body: string;
  created_at: string;
}

export interface Tag {
  name: string;
}

export interface Team {
  id: string;
  name: string;
  created_at: string;
}

export interface AdminSummary {
  id: string;
  email: string;
  name: string;
}

export interface Workspace {
  id: string;
  name: string;
  slug: string;
}

export interface Session {
  admin: AdminSummary;
  workspace: Workspace;
  role: Role;
}

/** POST /auth/login and /auth/refresh both return this. */
export interface TokenResponse extends Session {
  access_token: string;
  token_type: string;
  expires_in: number;
}
