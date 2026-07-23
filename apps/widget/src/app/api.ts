/**
 * Typed client for the widget BFF (`/v0/widget/*`). All calls send credentials so the
 * httpOnly lead cookie rides along; once booted, the session token is sent as a Bearer header.
 * Shapes mirror `relay.modules.messaging.schemas` (kept in sync via the generated SDK in CI).
 */

export interface BootUser {
  external_id?: string;
  email?: string;
  name?: string;
}

/** The config the loader hands the iframe via postMessage. */
export interface LoaderConfig {
  app_id: string;
  api_url?: string;
  host_origin?: string;
  user?: BootUser;
  user_hash?: string;
  theme?: { color?: string; position?: "left" | "right" };
}

export interface MessengerConfig {
  primary_color: string;
  launcher_position: "left" | "right";
  greeting: string | null;
  expected_reply_time: string | null;
  office_hours: Record<string, unknown> | null;
  identity_verification_enabled: boolean;
}

export interface Contact {
  id: string;
  kind: string;
  email: string | null;
  name: string | null;
}

export interface Conversation {
  id: string;
  contact_id: string;
  channel: string;
  state: string;
  waiting_since: string | null;
  last_part_at: string;
  created_at: string;
}

export interface Part {
  id: string;
  conversation_id: string;
  author_kind: string; // "contact" | "admin" | "ai_agent" | "system"
  author_id: string | null;
  part_type: string; // "comment" | "note" | "rating" | ...
  body: string | null;
  attachments: Array<Record<string, unknown>>;
  meta: Record<string, unknown>;
  created_at: string;
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

export interface BootResponse {
  session_token: string;
  contact: Contact;
  config: MessengerConfig;
  conversations: Conversation[];
}

export class RelayApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

const DEFAULT_API = "http://localhost:8000";

function rid(): string {
  // Idempotency key for retryable mutations. crypto.randomUUID is available in all iframe
  // targets; the fallback keeps it working under file:// / older embeds.
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export class RelayApi {
  private base: string;
  private token = "";

  constructor(apiUrl?: string) {
    this.base = (apiUrl ?? DEFAULT_API).replace(/\/+$/, "");
  }

  setToken(token: string): void {
    this.token = token;
  }

  private async request<T>(
    method: string,
    path: string,
    body?: unknown,
    extraHeaders?: Record<string, string>,
  ): Promise<T> {
    const headers: Record<string, string> = { "Content-Type": "application/json", ...extraHeaders };
    if (this.token) headers["Authorization"] = `Bearer ${this.token}`;
    const resp = await fetch(this.base + path, {
      method,
      headers,
      credentials: "include",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!resp.ok) {
      let code = "error";
      let message = resp.statusText;
      try {
        const payload = await resp.json();
        code = payload?.error?.code ?? code;
        message = payload?.error?.message ?? message;
      } catch {
        /* non-JSON error body */
      }
      throw new RelayApiError(resp.status, code, message);
    }
    return (resp.status === 204 ? null : await resp.json()) as T;
  }

  boot(cfg: LoaderConfig, resumeToken?: string): Promise<BootResponse> {
    return this.request<BootResponse>("POST", "/v0/widget/boot", {
      app_id: cfg.app_id,
      user: cfg.user,
      user_hash: cfg.user_hash,
      resume_token: resumeToken,
    });
  }

  listConversations(): Promise<Page<Conversation>> {
    return this.request("GET", "/v0/widget/conversations");
  }

  startConversation(body: string): Promise<Conversation> {
    return this.request("POST", "/v0/widget/conversations", { body }, { "Idempotency-Key": rid() });
  }

  listParts(convId: string, after?: string): Promise<Page<Part>> {
    const q = after ? `?after=${encodeURIComponent(after)}` : "";
    return this.request("GET", `/v0/widget/conversations/${convId}/parts${q}`);
  }

  reply(convId: string, body: string): Promise<Part> {
    return this.request(
      "POST",
      `/v0/widget/conversations/${convId}/reply`,
      { body },
      { "Idempotency-Key": rid() },
    );
  }

  rate(convId: string, rating: number, remark?: string): Promise<Part> {
    return this.request("POST", `/v0/widget/conversations/${convId}/rating`, { rating, remark });
  }

  typing(convId: string): Promise<null> {
    return this.request("POST", `/v0/widget/conversations/${convId}/typing`);
  }
}
