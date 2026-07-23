/**
 * Minimal, hand-written base client. Typed request/response models are generated from the
 * API's OpenAPI spec into `./generated/schema.ts` (`npm run generate`, wired in CI) and
 * layered on top of this transport. Every request is timed out (RFC master rules: external
 * calls always have timeouts).
 */

export interface ApiError {
  code: string;
  message: string;
  request_id?: string;
  details?: Record<string, unknown>;
}

export class RelayApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId?: string;

  constructor(status: number, body: { error?: ApiError }) {
    const err = body.error ?? { code: "unknown", message: "Unknown error" };
    super(err.message);
    this.name = "RelayApiError";
    this.status = status;
    this.code = err.code;
    this.requestId = err.request_id;
  }
}

export interface RelayClientOptions {
  baseUrl: string;
  /** Bearer token (agent access JWT or API key). */
  token?: string;
  /** Per-request timeout in ms (default 15s). */
  timeoutMs?: number;
  /** Injectable for tests / non-browser runtimes. */
  fetch?: typeof fetch;
}

export interface RequestOptions {
  method?: string;
  query?: Record<string, string | number | boolean | undefined>;
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
}

export class RelayClient {
  private readonly baseUrl: string;
  private readonly token?: string;
  private readonly timeoutMs: number;
  private readonly fetchImpl: typeof fetch;

  constructor(opts: RelayClientOptions) {
    this.baseUrl = opts.baseUrl.replace(/\/$/, "");
    this.token = opts.token;
    this.timeoutMs = opts.timeoutMs ?? 15_000;
    // Bind to the global: the browser's native `fetch` throws "Illegal invocation" if called
    // with `this` set to anything but the Window/global (which happens when stored on an instance
    // field and invoked as `this.fetchImpl(...)`).
    this.fetchImpl = opts.fetch ?? globalThis.fetch.bind(globalThis);
  }

  async request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
    const url = new URL(this.baseUrl + path);
    for (const [k, v] of Object.entries(opts.query ?? {})) {
      if (v !== undefined) url.searchParams.set(k, String(v));
    }

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.timeoutMs);
    const signal = opts.signal ?? controller.signal;

    try {
      const resp = await this.fetchImpl(url.toString(), {
        method: opts.method ?? "GET",
        headers: {
          "Content-Type": "application/json",
          ...(this.token ? { Authorization: `Bearer ${this.token}` } : {}),
          ...opts.headers,
        },
        body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
        signal,
        credentials: "include",
      });

      const text = await resp.text();
      const json = text ? JSON.parse(text) : undefined;
      if (!resp.ok) {
        throw new RelayApiError(resp.status, json ?? {});
      }
      return json as T;
    } finally {
      clearTimeout(timeout);
    }
  }

  /** Example call against the P0.0 hello-world endpoint. */
  hello(): Promise<{ message: string; service: string }> {
    return this.request("/v0/hello");
  }
}
