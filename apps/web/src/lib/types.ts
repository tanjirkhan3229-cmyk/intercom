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

// --- Knowledge / Help Center (P0.8) ------------------------------------------

export type ArticleStatus = "draft" | "published";

/** A block in an article's block-based body. Permissive so renderer/editor tolerate unknown
 * fields; the editor writes the fields below. */
export interface DocBlock {
  id: string;
  type: "paragraph" | "heading" | "list" | "code" | "callout" | "image";
  text?: string;
  level?: 2 | 3;
  items?: string[];
  ordered?: boolean;
  url?: string;
  alt?: string;
}

export interface ArticleBody {
  blocks: DocBlock[];
}

export interface Collection {
  id: string;
  slug: string;
  name: string;
  description: string | null;
  icon: string | null;
  position: number;
  parent_id: string | null;
  article_count: number;
  created_at: string;
  updated_at: string;
}

export interface Article {
  id: string;
  collection_id: string | null;
  slug: string;
  title: string;
  body: ArticleBody;
  status: ArticleStatus;
  locale: string;
  seo_title: string | null;
  seo_description: string | null;
  author_id: string | null;
  position: number;
  published_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ArticleSummary {
  id: string;
  collection_id: string | null;
  slug: string;
  title: string;
  status: ArticleStatus;
  position: number;
  updated_at: string;
  published_at: string | null;
}

export interface HelpCenterConfig {
  name: string | null;
  logo_url: string | null;
  primary_color: string | null;
  custom_domain: string | null;
  default_locale: string;
  updated_at: string | null;
}

// Public (hosted site + widget) — published content only.

export interface PublicArticle {
  id: string;
  slug: string;
  title: string;
  body: ArticleBody;
  seo_title: string | null;
  seo_description: string | null;
  collection_slug: string | null;
  published_at: string | null;
  updated_at: string;
}

export interface PublicArticleSummary {
  id: string;
  slug: string;
  title: string;
  excerpt: string;
  collection_slug: string | null;
  updated_at: string;
}

export interface PublicCollection {
  slug: string;
  name: string;
  description: string | null;
  icon: string | null;
  articles: PublicArticleSummary[];
}

export interface PublicCollectionSummary {
  slug: string;
  name: string;
  description: string | null;
  icon: string | null;
  article_count: number;
}

export interface PublicHelpCenter {
  workspace_slug: string;
  name: string;
  logo_url: string | null;
  primary_color: string | null;
  default_locale: string;
  collections: PublicCollectionSummary[];
}

export interface PublicSearchResult {
  slug: string;
  title: string;
  excerpt: string;
  collection_slug: string | null;
  rank: number;
}

export interface PublicSearchResponse {
  query: string;
  results: PublicSearchResult[];
}

/** Editor create/update payloads. */
export interface CollectionInput {
  name?: string;
  slug?: string;
  description?: string | null;
  icon?: string | null;
  position?: number;
  parent_id?: string | null;
}

export interface ArticleInput {
  title?: string;
  slug?: string;
  collection_id?: string | null;
  body?: ArticleBody;
  seo_title?: string | null;
  seo_description?: string | null;
  position?: number;
}

export interface HelpCenterInput {
  name?: string;
  logo_url?: string | null;
  primary_color?: string | null;
  default_locale?: string;
}
