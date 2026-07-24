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

// --- Knowledge Hub sources (P1.1) ---------------------------------------------

export type SourceKind = "url" | "pdf" | "snippet";
/** Per-source AI-readiness surfaced in the UI. */
export type SourceStatus = "pending" | "syncing" | "synced" | "error";

export interface Source {
  id: string;
  kind: SourceKind;
  title: string;
  status: SourceStatus;
  config: Record<string, unknown>;
  locale: string;
  audience: Record<string, unknown>;
  document_count: number;
  chunk_count: number;
  last_synced_at: string | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
}

export interface SourceInput {
  kind: SourceKind;
  title: string;
  config?: Record<string, unknown>;
  locale?: string;
  audience?: Record<string, unknown>;
}

// --- Neko AI agent (P1.3) -----------------------------------------------------

export type NekoTone = "friendly" | "neutral" | "formal";
export type OfficeHoursBehavior = "answer" | "handoff";

/** Per-workspace Neko controls (RFC-003 §5-6, §9). Mirrors the backend `AiSettingsOut`. */
export interface AiSettings {
  enabled: boolean;
  channels: string[];
  grounding_threshold: number;
  max_clarifications: number;
  source_kinds: string[] | null;
  tone: NekoTone;
  persona: string | null;
  answer_max_tokens: number;
  always_handoff_intents: string[];
  office_hours_behavior: OfficeHoursBehavior;
  monthly_spend_cap_usd: number | null;
}

export type AiSettingsInput = Partial<AiSettings>;

/** Month-to-date resolution usage + spend-cap status (RFC-003 §9). */
export interface NekoUsage {
  month_resolutions: number;
  month_spend_usd: number;
  monthly_spend_cap_usd: number | null;
  over_cap: boolean;
}

/** One retrieved chunk in a preview trace — the evidence + its fused score. */
export interface RetrievedChunk {
  label?: string;
  chunk_id: string;
  source_id: string;
  source_kind: string;
  score: number;
}

/** A preview-sandbox turn (RFC-003 §5): same decisions + retrieval trace as a real turn. */
export interface SandboxTurn {
  outcome: string;
  handoff_reason: string | null;
  rewritten_query: string | null;
  retrieved: RetrievedChunk[];
  grounding_score: number | null;
  citations: string[];
  verdict: Record<string, unknown>;
  answer: string | null;
  prompt_hash: string | null;
  provider: string | null;
  models: Record<string, unknown>;
  tokens: Record<string, unknown>;
  cost_usd: number;
  latency_ms: Record<string, unknown>;
  trace: Record<string, unknown>;
}

export interface SandboxTurnInput {
  message: string;
  history?: { role: "customer" | "neko"; body: string }[];
}
