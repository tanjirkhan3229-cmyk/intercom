import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import {
  createRealtimeChannel,
  type LiveTransport,
  type RealtimeChannel,
  type RealtimeMessage,
} from "@relay/shared";
import {
  RelayApi,
  RelayApiError,
  type Conversation,
  type LoaderConfig,
  type MessengerConfig,
  type Part,
} from "./api";
import { setLocale, t } from "./i18n";

const RESUME_KEY = "relay_session";

// Reload continuity fallback when third-party storage is blocked (Safari): best-effort only.
function lsGet(key: string): string | undefined {
  try {
    return localStorage.getItem(key) ?? undefined;
  } catch {
    return undefined;
  }
}
function lsSet(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* storage blocked/partitioned — the httpOnly cookie is the primary resume path */
  }
}

// ponytail: no Centrifugo browser SDK in the bundle yet (50 KB budget). This transport reports
// "down" immediately, so the shared controller runs in its long-poll mode (dedupe + jittered
// live-retry intact). Swap in the `centrifuge` wiring from packages/shared/src/realtime.ts to
// light up push + typing without touching this component.
const pollOnlyTransport: LiveTransport = {
  subscribe(_channel, handlers) {
    handlers.onError(new Error("live transport not bundled"));
    return () => {};
  },
};

export function App() {
  const [status, setStatus] = useState<"booting" | "ready" | "error">("booting");
  const [config, setConfig] = useState<MessengerConfig | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [view, setView] = useState<"home" | "thread">("home");
  const [active, setActive] = useState<Conversation | null>(null);
  const [parts, setParts] = useState<Part[]>([]);
  const [draft, setDraft] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [sending, setSending] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [ratingDone, setRatingDone] = useState(false);

  const apiRef = useRef<RelayApi | null>(null);
  const hostOriginRef = useRef("*");
  const openRef = useRef(false);
  const unreadRef = useRef(0);
  const channelRef = useRef<RealtimeChannel | null>(null);
  const lastTypingRef = useRef(0);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  function toParent(msg: Record<string, unknown>): void {
    window.parent.postMessage(msg, hostOriginRef.current);
  }

  function bumpUnread(part: Part): void {
    if (part.author_kind !== "contact" && !openRef.current) {
      unreadRef.current += 1;
      toParent({ type: "relay:unread", count: unreadRef.current });
    }
  }

  function resetUnread(): void {
    if (unreadRef.current === 0) return;
    unreadRef.current = 0;
    toParent({ type: "relay:unread", count: 0 });
  }

  function ingest(part: Part): void {
    setParts((prev) => (prev.some((p) => p.id === part.id) ? prev : [...prev, part]));
    bumpUnread(part);
  }

  async function doBoot(cfg: LoaderConfig): Promise<void> {
    if (apiRef.current) return; // boot once per load
    hostOriginRef.current = cfg.host_origin || "*";
    setLocale(navigator.language);
    const api = new RelayApi(cfg.api_url);
    apiRef.current = api;
    try {
      const res = await api.boot(cfg, lsGet(RESUME_KEY));
      api.setToken(res.session_token);
      lsSet(RESUME_KEY, res.session_token);
      document.documentElement.style.setProperty("--relay-primary", res.config.primary_color);
      setConfig(res.config);
      setConversations(res.conversations);
      toParent({
        type: "relay:config",
        color: res.config.primary_color,
        position: res.config.launcher_position,
      });
      setStatus("ready");
    } catch (e) {
      setErrorMsg(e instanceof RelayApiError ? e.message : t("error_generic"));
      setStatus("error");
    }
  }

  // Boot handshake + visibility, once.
  useEffect(() => {
    function onMsg(ev: MessageEvent): void {
      const data = ev.data as { type?: string; config?: LoaderConfig; open?: boolean };
      if (!data || typeof data !== "object") return;
      if (data.type === "relay:boot" && ev.source === window.parent && data.config) {
        void doBoot(data.config);
      } else if (data.type === "relay:visibility") {
        openRef.current = !!data.open;
        if (data.open) resetUnread();
      }
    }
    window.addEventListener("message", onMsg);
    // Standalone/dev: boot straight from query params (?app_id=wrk_…&api_url=…) when there's no
    // embedding loader. In production the loader always posts relay:boot instead.
    const params = new URLSearchParams(location.search);
    const appId = params.get("app_id");
    if (appId) {
      void doBoot({
        app_id: appId,
        api_url: params.get("api_url") ?? undefined,
        host_origin: "*",
      });
    } else {
      window.parent.postMessage({ type: "relay:ready" }, "*"); // loader validates source+origin
    }
    return () => {
      window.removeEventListener("message", onMsg);
      channelRef.current?.stop();
    };
  }, []);

  // Keep the thread scrolled to the newest message.
  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [parts, view]);

  function startChannel(convId: string, lastId: string | undefined): void {
    const api = apiRef.current;
    if (!api) return;
    channelRef.current?.stop();
    const ch = createRealtimeChannel({
      channel: `conv:${convId}`,
      transport: pollOnlyTransport,
      poll: async (after) =>
        (await api.listParts(convId, after)).items as unknown as RealtimeMessage[],
      onEvent: (msg) => ingest(msg as unknown as Part),
      initialLastId: lastId,
      pollIntervalMs: 3000,
    });
    channelRef.current = ch;
    ch.start();
  }

  async function openConversation(conv: Conversation): Promise<void> {
    const api = apiRef.current;
    if (!api) return;
    setActive(conv);
    setView("thread");
    setRatingDone(false);
    setParts([]);
    try {
      const page = await api.listParts(conv.id); // newest-first
      const ascending = page.items.slice().reverse();
      setParts(ascending);
      startChannel(conv.id, ascending[ascending.length - 1]?.id);
    } catch (e) {
      setErrorMsg(e instanceof RelayApiError ? e.message : t("error_generic"));
    }
  }

  function leaveThread(): void {
    channelRef.current?.stop();
    channelRef.current = null;
    setActive(null);
    setView("home");
  }

  async function startNew(): Promise<void> {
    const body = draft.trim();
    const api = apiRef.current;
    if (!body || !api || sending) return;
    setSending(true);
    setErrorMsg(null);
    try {
      const conv = await api.startConversation(body);
      setConversations((prev) => [conv, ...prev]);
      setDraft("");
      setFiles([]);
      await openConversation(conv);
    } catch (e) {
      setErrorMsg(e instanceof RelayApiError ? e.message : t("error_generic"));
    } finally {
      setSending(false);
    }
  }

  async function sendReply(): Promise<void> {
    const body = draft.trim();
    const api = apiRef.current;
    if (!body || !api || !active || sending) return;
    setSending(true);
    setErrorMsg(null);
    setDraft("");
    setFiles([]);
    try {
      ingest(await api.reply(active.id, body));
    } catch (e) {
      setDraft(body); // let the visitor retry
      setErrorMsg(e instanceof RelayApiError ? e.message : t("error_generic"));
    } finally {
      setSending(false);
    }
  }

  function onDraftInput(value: string): void {
    setDraft(value);
    const api = apiRef.current;
    const now = Date.now();
    // Throttle typing pings to the TTL window; the server relays them (agents see them live).
    if (api && active && now - lastTypingRef.current > 2500) {
      lastTypingRef.current = now;
      void api.typing(active.id).catch(() => {});
    }
  }

  async function submitRating(rating: number): Promise<void> {
    const api = apiRef.current;
    if (!api || !active) return;
    try {
      ingest(await api.rate(active.id, rating));
      setRatingDone(true);
    } catch {
      setErrorMsg(t("error_generic"));
    }
  }

  const { isClosed, hasRating } = useMemo(() => {
    let closed = false;
    for (const p of parts) {
      if (p.part_type === "state_change") closed = (p.meta?.to as string) === "closed";
    }
    return { isClosed: closed, hasRating: parts.some((p) => p.part_type === "rating") };
  }, [parts]);

  if (status === "booting") return <Splash label="" />;
  if (status === "error") return <Splash label={errorMsg ?? t("error_generic")} error />;

  return (
    <div class="rl-root">
      <header class="rl-header">
        {view === "thread" && (
          <button class="rl-icon-btn" aria-label={t("back")} onClick={leaveThread}>
            ‹
          </button>
        )}
        <div class="rl-header-text">
          <div class="rl-title">{t("header_default")}</div>
          {config?.expected_reply_time && (
            <div class="rl-subtitle">
              {t("reply_time_prefix")} {config.expected_reply_time}
            </div>
          )}
        </div>
      </header>

      {view === "home" ? (
        <Home
          config={config}
          conversations={conversations}
          onOpen={openConversation}
          draft={draft}
          setDraft={setDraft}
          onStart={startNew}
          sending={sending}
        />
      ) : (
        <>
          <div class="rl-thread" ref={scrollRef}>
            {parts.length === 0 && <p class="rl-empty">{t("empty_thread")}</p>}
            {parts.map((p) => (
              <Bubble key={p.id} part={p} />
            ))}
            {isClosed && !hasRating && !ratingDone && <RatingPrompt onRate={submitRating} />}
            {(hasRating || ratingDone) && <p class="rl-system">{t("rate_thanks")}</p>}
          </div>
          {errorMsg && <p class="rl-error">{errorMsg}</p>}
          <Composer
            draft={draft}
            onInput={onDraftInput}
            onSend={sendReply}
            files={files}
            setFiles={setFiles}
            disabled={sending}
          />
        </>
      )}
    </div>
  );
}

function Splash({ label, error }: { label: string; error?: boolean }) {
  return (
    <div class="rl-root rl-splash">
      {error ? <p class="rl-error">{label}</p> : <div class="rl-spinner" aria-label="Loading" />}
    </div>
  );
}

function Home(props: {
  config: MessengerConfig | null;
  conversations: Conversation[];
  onOpen: (c: Conversation) => void;
  draft: string;
  setDraft: (v: string) => void;
  onStart: () => void;
  sending: boolean;
}) {
  const greeting = props.config?.greeting || t("greeting_default");
  return (
    <div class="rl-home">
      <p class="rl-greeting">{greeting}</p>
      <div class="rl-newconv">
        <textarea
          class="rl-input"
          rows={2}
          placeholder={t("start_conversation")}
          value={props.draft}
          disabled={props.sending}
          onInput={(e) => props.setDraft((e.target as HTMLTextAreaElement).value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              props.onStart();
            }
          }}
        />
        <button
          class="rl-send"
          onClick={props.onStart}
          disabled={props.sending || !props.draft.trim()}
        >
          {t("send")}
        </button>
      </div>
      {props.conversations.length > 0 && (
        <div class="rl-list">
          <div class="rl-list-label">{t("recent")}</div>
          {props.conversations.map((c) => (
            <button key={c.id} class="rl-list-item" onClick={() => props.onOpen(c)}>
              <span class="rl-list-title">{t("new_conversation")}</span>
              <span class="rl-list-time">{fmtTime(c.last_part_at)}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function Bubble({ part }: { part: Part }) {
  if (part.part_type === "state_change") {
    const to = part.meta?.to as string | undefined;
    return to === "closed" ? <p class="rl-system">{t("closed_note")}</p> : null;
  }
  if (part.part_type === "rating") {
    const n = Number(part.meta?.rating) || 0;
    return <p class="rl-system">{"★".repeat(n)}</p>;
  }
  const mine = part.author_kind === "contact";
  const atts = part.attachments ?? [];
  return (
    <div class={`rl-msg ${mine ? "rl-mine" : "rl-theirs"}`}>
      <div class="rl-bubble">
        {part.body}
        {atts.map((a, i) => (
          <div key={i} class="rl-att">
            📎 {(a.name as string) ?? "attachment"}
          </div>
        ))}
      </div>
      <div class="rl-meta">
        {fmtTime(part.created_at)}
        {mine && (
          <span class="rl-check" title={t("delivered")}>
            {" "}
            · ✓
          </span>
        )}
      </div>
    </div>
  );
}

function RatingPrompt({ onRate }: { onRate: (n: number) => void }) {
  return (
    <div class="rl-rating">
      <p>{t("rate_prompt")}</p>
      <div class="rl-stars">
        {[1, 2, 3, 4, 5].map((n) => (
          <button key={n} aria-label={`${n}`} class="rl-star" onClick={() => onRate(n)}>
            ★
          </button>
        ))}
      </div>
    </div>
  );
}

function Composer(props: {
  draft: string;
  onInput: (v: string) => void;
  onSend: () => void;
  files: File[];
  setFiles: (f: File[]) => void;
  disabled: boolean;
}) {
  const fileRef = useRef<HTMLInputElement | null>(null);
  return (
    <footer class="rl-composer">
      {props.files.length > 0 && (
        <div class="rl-chips">
          {props.files.map((f, i) => (
            <span key={i} class="rl-chip">
              📎 {f.name}
              <button
                aria-label="Remove"
                onClick={() => props.setFiles(props.files.filter((_, j) => j !== i))}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}
      <div class="rl-composer-row">
        {/* ponytail: file bytes need the presigned-upload endpoint (lands with S3 attachments,
            shared with P0.7). For now the affordance selects + lists files; wire the PUT there. */}
        <button
          class="rl-icon-btn"
          aria-label={t("attach")}
          onClick={() => fileRef.current?.click()}
          disabled={props.disabled}
        >
          📎
        </button>
        <input
          ref={fileRef}
          type="file"
          multiple
          hidden
          onChange={(e) => {
            const list = (e.target as HTMLInputElement).files;
            if (list) props.setFiles([...props.files, ...Array.from(list)]);
          }}
        />
        <textarea
          class="rl-input"
          rows={1}
          placeholder={t("composer_placeholder")}
          value={props.draft}
          disabled={props.disabled}
          onInput={(e) => props.onInput((e.target as HTMLTextAreaElement).value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              props.onSend();
            }
          }}
        />
        <button
          class="rl-send"
          onClick={props.onSend}
          disabled={props.disabled || !props.draft.trim()}
        >
          {t("send")}
        </button>
      </div>
    </footer>
  );
}

function fmtTime(iso: string): string {
  const d = new Date(iso);
  return isNaN(d.getTime())
    ? ""
    : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
