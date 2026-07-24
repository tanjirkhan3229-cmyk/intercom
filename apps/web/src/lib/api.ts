import { RelayClient, RelayApiError } from "@relay/sdk-ts";
import type { Page } from "@relay/shared";
import type {
  AttributeDefinition,
  Workflow,
  WorkflowCreateInput,
  WorkflowGraph,
  WorkflowPatchInput,
  WorkflowRun,
  WorkflowRunStep,
  WorkflowSummary,
  WorkflowVersion,
} from "./workflows/contract";
import type {
  Article,
  ArticleInput,
  ArticleSummary,
  Collection,
  CollectionInput,
  Conversation,
  Contact,
  ContactEvent,
  HelpCenterConfig,
  HelpCenterInput,
  Part,
  SavedReply,
  Session,
  Tag,
  Team,
  TokenResponse,
} from "./types";

export { RelayApiError };

const BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

/** A fresh idempotency key per mutating call (RFC master rule: mutating endpoints are idempotent). */
function idemKey(): string {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
}

/**
 * Typed façade over the `@relay/sdk-ts` transport. One instance is bound to the current access
 * token; the auth layer rebuilds it on token change (see `lib/auth`). `credentials: "include"`
 * (set in the SDK) carries the httpOnly refresh cookie for `/auth/refresh` + `/auth/logout`.
 */
export class RelayApi {
  private readonly client: RelayClient;

  constructor(token?: string) {
    this.client = new RelayClient({ baseUrl: BASE_URL, token });
  }

  // --- Auth (identity, P0.1) --------------------------------------------------
  login(email: string, password: string, workspaceId?: string): Promise<TokenResponse> {
    return this.client.request<TokenResponse>("/v0/auth/login", {
      method: "POST",
      body: { email, password, workspace_id: workspaceId },
    });
  }
  refresh(): Promise<TokenResponse> {
    return this.client.request<TokenResponse>("/v0/auth/refresh", { method: "POST" });
  }
  logout(): Promise<void> {
    return this.client.request<void>("/v0/auth/logout", { method: "POST" });
  }
  me(): Promise<Session> {
    return this.client.request<Session>("/v0/auth/me");
  }
  teams(): Promise<Team[]> {
    return this.client.request<Team[]>("/v0/teams");
  }

  // --- Conversations (messaging, P0.3 + P0.5 reads) ---------------------------
  listConversations(params: {
    state?: string;
    teamId?: string;
    assigneeId?: string;
    unassigned?: boolean;
    cursor?: string;
    limit?: number;
  }): Promise<Page<Conversation>> {
    return this.client.request<Page<Conversation>>("/v0/conversations", {
      query: {
        state: params.state,
        team_id: params.teamId,
        assignee_id: params.assigneeId,
        unassigned: params.unassigned,
        cursor: params.cursor,
        limit: params.limit,
      },
    });
  }
  getConversation(id: string): Promise<Conversation> {
    return this.client.request<Conversation>(`/v0/conversations/${id}`);
  }
  listContactConversations(contactId: string, cursor?: string): Promise<Page<Conversation>> {
    return this.client.request<Page<Conversation>>(`/v0/contacts/${contactId}/conversations`, {
      query: { cursor },
    });
  }

  // --- Thread parts -----------------------------------------------------------
  listParts(id: string, cursor?: string): Promise<Page<Part>> {
    return this.client.request<Page<Part>>(`/v0/conversations/${id}/parts`, { query: { cursor } });
  }
  /** Realtime long-poll fallback: parts newer than `after`, ascending (RFC-001 §6.3). */
  listPartsAfter(id: string, after?: string): Promise<Page<Part>> {
    return this.client.request<Page<Part>>(`/v0/conversations/${id}/parts`, { query: { after } });
  }

  // --- Mutations (idempotent) -------------------------------------------------
  reply(id: string, body: string, attachments: unknown[] = []): Promise<Part> {
    return this.client.request<Part>(`/v0/conversations/${id}/reply`, {
      method: "POST",
      headers: { "Idempotency-Key": idemKey() },
      body: { body, attachments },
    });
  }
  note(id: string, body: string): Promise<Part> {
    return this.client.request<Part>(`/v0/conversations/${id}/note`, {
      method: "POST",
      headers: { "Idempotency-Key": idemKey() },
      body: { body },
    });
  }
  setState(id: string, state: string, snoozedUntil?: string): Promise<Conversation> {
    return this.client.request<Conversation>(`/v0/conversations/${id}/state`, {
      method: "POST",
      headers: { "Idempotency-Key": idemKey() },
      body: { state, snoozed_until: snoozedUntil },
    });
  }
  assign(id: string, assigneeId?: string | null, teamId?: string | null): Promise<Conversation> {
    return this.client.request<Conversation>(`/v0/conversations/${id}/assign`, {
      method: "POST",
      headers: { "Idempotency-Key": idemKey() },
      body: { assignee_id: assigneeId, team_id: teamId },
    });
  }

  // --- Tags -------------------------------------------------------------------
  listTags(id: string): Promise<Tag[]> {
    return this.client.request<Tag[]>(`/v0/conversations/${id}/tags`);
  }
  addTag(id: string, name: string): Promise<void> {
    return this.client.request<void>(`/v0/conversations/${id}/tags`, {
      method: "POST",
      body: { name },
    });
  }
  removeTag(id: string, name: string): Promise<void> {
    return this.client.request<void>(
      `/v0/conversations/${id}/tags/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    );
  }

  // --- Saved replies (macros) -------------------------------------------------
  listSavedReplies(): Promise<SavedReply[]> {
    return this.client.request<SavedReply[]>("/v0/saved-replies");
  }

  // --- Attachment uploads (presigned S3, platform) ----------------------------
  presignUpload(
    filename: string,
    contentType: string,
  ): Promise<{ key: string; upload_url: string; method: string }> {
    return this.client.request<{ key: string; upload_url: string; method: string }>(
      "/v0/uploads/presign",
      { method: "POST", body: { filename, content_type: contentType } },
    );
  }
  attachmentDownloadUrl(key: string): Promise<{ url: string }> {
    return this.client.request<{ url: string }>("/v0/uploads/download-url", { query: { key } });
  }

  // --- Contacts (crm) ---------------------------------------------------------
  getContact(id: string): Promise<Contact> {
    return this.client.request<Contact>(`/v0/contacts/${id}`);
  }
  listContactEvents(id: string, limit = 20): Promise<ContactEvent[]> {
    return this.client.request<ContactEvent[]>(`/v0/contacts/${id}/events`, { query: { limit } });
  }

  // --- Realtime tokens (Centrifugo, P0.4) -------------------------------------
  realtimeToken(): Promise<{ token: string; ws_url: string }> {
    return this.client.request<{ token: string; ws_url: string }>("/v0/realtime/token", {
      method: "POST",
    });
  }
  realtimeSubscribe(
    channels: string[],
  ): Promise<{ tokens: Record<string, string>; ws_url: string }> {
    return this.client.request<{ tokens: Record<string, string>; ws_url: string }>(
      "/v0/realtime/subscribe",
      { method: "POST", body: { channels } },
    );
  }
  presence(): Promise<void> {
    return this.client.request<void>("/v0/realtime/presence", { method: "POST" });
  }
  typing(id: string): Promise<void> {
    return this.client.request<void>(`/v0/conversations/${id}/typing`, {
      method: "POST",
      body: { typing: true },
    });
  }

  // --- Help Center: collections (knowledge, P0.8) -----------------------------
  listCollections(): Promise<Collection[]> {
    return this.client.request<Collection[]>("/v0/collections");
  }
  createCollection(input: CollectionInput): Promise<Collection> {
    return this.client.request<Collection>("/v0/collections", { method: "POST", body: input });
  }
  updateCollection(id: string, input: CollectionInput): Promise<Collection> {
    return this.client.request<Collection>(`/v0/collections/${id}`, {
      method: "PATCH",
      body: input,
    });
  }
  deleteCollection(id: string): Promise<void> {
    return this.client.request<void>(`/v0/collections/${id}`, { method: "DELETE" });
  }

  // --- Help Center: articles --------------------------------------------------
  listArticles(
    params: { status?: string; collectionId?: string; cursor?: string; limit?: number } = {},
  ): Promise<Page<ArticleSummary>> {
    return this.client.request<Page<ArticleSummary>>("/v0/articles", {
      query: {
        status: params.status,
        collection_id: params.collectionId,
        cursor: params.cursor,
        limit: params.limit,
      },
    });
  }
  getArticle(id: string): Promise<Article> {
    return this.client.request<Article>(`/v0/articles/${id}`);
  }
  createArticle(input: ArticleInput): Promise<Article> {
    return this.client.request<Article>("/v0/articles", { method: "POST", body: input });
  }
  updateArticle(id: string, input: ArticleInput): Promise<Article> {
    return this.client.request<Article>(`/v0/articles/${id}`, { method: "PATCH", body: input });
  }
  deleteArticle(id: string): Promise<void> {
    return this.client.request<void>(`/v0/articles/${id}`, { method: "DELETE" });
  }
  publishArticle(id: string): Promise<Article> {
    return this.client.request<Article>(`/v0/articles/${id}/publish`, { method: "POST" });
  }
  unpublishArticle(id: string): Promise<Article> {
    return this.client.request<Article>(`/v0/articles/${id}/unpublish`, { method: "POST" });
  }

  // --- Help Center: config ----------------------------------------------------
  getHelpCenter(): Promise<HelpCenterConfig> {
    return this.client.request<HelpCenterConfig>("/v0/help-center");
  }
  updateHelpCenter(input: HelpCenterInput): Promise<HelpCenterConfig> {
    return this.client.request<HelpCenterConfig>("/v0/help-center", {
      method: "PATCH",
      body: input,
    });
  }

  // --- Workflows (automation, P1.6 builder ↔ P1.5 engine) ---------------------
  // Contract: src/lib/workflows/contract.md. This façade is the single seam that isolates the
  // REST envelope — if the P1.5 backend lands with minor differences, only this section changes.
  listWorkflows(params: { cursor?: string; limit?: number; status?: string } = {}): Promise<
    Page<WorkflowSummary>
  > {
    return this.client.request<Page<WorkflowSummary>>("/v0/workflows", {
      query: { cursor: params.cursor, limit: params.limit, status: params.status },
    });
  }
  createWorkflow(input: WorkflowCreateInput): Promise<Workflow> {
    return this.client.request<Workflow>("/v0/workflows", {
      method: "POST",
      headers: { "Idempotency-Key": idemKey() },
      body: input,
    });
  }
  getWorkflow(id: string): Promise<Workflow> {
    return this.client.request<Workflow>(`/v0/workflows/${id}`);
  }
  patchWorkflow(id: string, input: WorkflowPatchInput): Promise<Workflow> {
    return this.client.request<Workflow>(`/v0/workflows/${id}`, {
      method: "PATCH",
      headers: { "Idempotency-Key": idemKey() },
      body: input,
    });
  }
  deleteWorkflow(id: string): Promise<void> {
    return this.client.request<void>(`/v0/workflows/${id}`, {
      method: "DELETE",
      headers: { "Idempotency-Key": idemKey() },
    });
  }
  /** Idempotent full-graph replace of the draft version (server does not validate a draft). */
  saveDraft(id: string, graph: WorkflowGraph): Promise<WorkflowVersion> {
    return this.client.request<WorkflowVersion>(`/v0/workflows/${id}/draft`, {
      method: "PUT",
      headers: { "Idempotency-Key": idemKey() },
      body: { graph },
    });
  }
  /** Validate + freeze a new immutable published version. Throws RelayApiError(422) on an invalid
   * graph; the UI surfaces `err.message` (RelayApiError does not expose `details.path`, so the rich
   * per-node mapping comes from the client-side validator, which blocks publish pre-flight). */
  publishWorkflow(id: string, graph: WorkflowGraph): Promise<WorkflowVersion> {
    return this.client.request<WorkflowVersion>(`/v0/workflows/${id}/publish`, {
      method: "POST",
      headers: { "Idempotency-Key": idemKey() },
      body: { graph },
    });
  }
  listWorkflowVersions(id: string, cursor?: string): Promise<Page<WorkflowVersion>> {
    return this.client.request<Page<WorkflowVersion>>(`/v0/workflows/${id}/versions`, {
      query: { cursor },
    });
  }
  getWorkflowVersion(id: string, versionId: string): Promise<WorkflowVersion> {
    return this.client.request<WorkflowVersion>(`/v0/workflows/${id}/versions/${versionId}`);
  }
  listWorkflowRuns(
    id: string,
    params: { status?: string; versionId?: string; cursor?: string; limit?: number } = {},
  ): Promise<Page<WorkflowRun>> {
    return this.client.request<Page<WorkflowRun>>(`/v0/workflows/${id}/runs`, {
      query: {
        status: params.status,
        version_id: params.versionId,
        cursor: params.cursor,
        limit: params.limit,
      },
    });
  }
  getWorkflowRun(runId: string): Promise<WorkflowRun> {
    return this.client.request<WorkflowRun>(`/v0/workflows/runs/${runId}`);
  }
  listWorkflowRunSteps(runId: string): Promise<WorkflowRunStep[]> {
    return this.client.request<WorkflowRunStep[]>(`/v0/workflows/runs/${runId}/steps`);
  }
  rerunFromStep(runId: string, fromNodeId: string): Promise<WorkflowRun> {
    return this.client.request<WorkflowRun>(`/v0/workflows/runs/${runId}/rerun`, {
      method: "POST",
      headers: { "Idempotency-Key": idemKey() },
      body: { from_node_id: fromNodeId },
    });
  }

  // --- Attribute definitions (crm, reused by the predicate field picker) ------
  listAttributeDefinitions(entity: "contact" | "company"): Promise<AttributeDefinition[]> {
    return this.client.request<AttributeDefinition[]>("/v0/attribute-definitions", {
      query: { entity },
    });
  }
}
