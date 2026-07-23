import { RelayClient, RelayApiError } from "@relay/sdk-ts";
import type { Page } from "@relay/shared";
import type {
  Conversation,
  Contact,
  ContactEvent,
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
}
