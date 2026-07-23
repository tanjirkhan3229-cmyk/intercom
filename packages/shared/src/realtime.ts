/**
 * Realtime channel with automatic long-poll fallback (RFC-001 §6.3, §9).
 *
 * Both the agent app (P0.5) and the widget (P0.6) drive their thread cache through this. It is
 * deliberately **transport-agnostic**: you inject the live transport (a thin wrapper over the
 * Centrifugo `centrifuge` browser SDK — Relay *buys* realtime, RFC-001 §6.1) and a `poll`
 * function (the `GET /conversations/:id/parts?after=` fallback). The controller owns the three
 * behaviours the acceptance criteria pin down:
 *
 *   1. **Auto-downgrade.** On websocket failure it switches to polling — no message loss, because
 *      parts are read straight from Postgres (the outbox/DB is the source of truth), so whatever
 *      the fanout couldn't push is replayed on the next poll.
 *   2. **Jittered reconnect.** While polling it keeps retrying the live transport with full-jitter
 *      exponential backoff, so a gateway restart doesn't produce a thundering reconnect herd.
 *   3. **Exactly-once effect.** Every event is deduped by part id, so the live path, the poll path,
 *      and outbox at-least-once redeliveries all collapse to one render per part.
 *
 * Wiring the live transport (P0.5/P0.6, once `centrifuge` is a dep):
 *
 *   import { Centrifuge } from "centrifuge";
 *   const transport: LiveTransport = {
 *     subscribe(channel, h) {
 *       const c = new Centrifuge(wsUrl, { getToken: fetchConnectionToken });
 *       const sub = c.newSubscription(channel, { getToken: () => fetchSubscribeToken(channel) });
 *       sub.on("publication", (ctx) => h.onMessage(ctx.data as RealtimeMessage));
 *       sub.on("subscribed", () => h.onLive());
 *       sub.on("error", (e) => h.onError(e));
 *       c.on("disconnected", (e) => h.onError(e));
 *       sub.subscribe(); c.connect();
 *       return () => { sub.unsubscribe(); c.disconnect(); };
 *     },
 *   };
 */

export type RealtimeStatus = "connecting" | "live" | "polling" | "closed";

/** A message off a channel: conversation events carry `part_id`; poll results carry `id` — both
 * are the same `msg_` part id, which is why dedupe works across the two transports. */
export interface RealtimeMessage {
  topic?: string;
  part_id?: string;
  id?: string;
  [key: string]: unknown;
}

export interface LiveHandlers {
  /** Called once the live subscription is established (stops the polling fallback). */
  onLive: () => void;
  /** Called per message received live. */
  onMessage: (msg: RealtimeMessage) => void;
  /** Called on any websocket/subscription failure (triggers downgrade to polling). */
  onError: (err: unknown) => void;
}

export interface LiveTransport {
  /** Subscribe to `channel`. Returns an unsubscribe/teardown function. */
  subscribe(channel: string, handlers: LiveHandlers): () => void;
}

export interface RealtimeOptions {
  channel: string;
  transport: LiveTransport;
  /** Long-poll fallback: return parts newer than `after` (the newest part id held), ascending. */
  poll: (after: string | undefined) => Promise<RealtimeMessage[]>;
  /** Called for every fresh (deduped) message, from either transport. */
  onEvent: (msg: RealtimeMessage) => void;
  onStatus?: (status: RealtimeStatus) => void;
  /** Newest part id already held, so the first poll doesn't re-fetch the whole thread. */
  initialLastId?: string;
  pollIntervalMs?: number;
  backoffBaseMs?: number;
  backoffMaxMs?: number;
  /** Injectable for tests. */
  now?: () => number;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (handle: unknown) => void;
  random?: () => number;
}

const DEDUPE_WINDOW = 1000;

/** Full-jitter exponential backoff (AWS "Exponential Backoff and Jitter"): a stable gateway
 * restart won't sync every client onto the same reconnect instant. */
export function jitteredBackoff(
  attempt: number,
  baseMs: number,
  maxMs: number,
  random: () => number,
): number {
  const ceil = Math.min(maxMs, baseMs * 2 ** attempt);
  return Math.floor(ceil * (0.5 + random() * 0.5));
}

export interface RealtimeChannel {
  start: () => void;
  stop: () => void;
  getStatus: () => RealtimeStatus;
}

export function createRealtimeChannel(opts: RealtimeOptions): RealtimeChannel {
  const pollIntervalMs = opts.pollIntervalMs ?? 10_000; // RFC-001 §6.3: ~10 s degraded polling
  const backoffBaseMs = opts.backoffBaseMs ?? 1_000;
  const backoffMaxMs = opts.backoffMaxMs ?? 30_000;
  const setTimer = opts.setTimer ?? ((fn, ms) => setTimeout(fn, ms));
  const clearTimer = opts.clearTimer ?? ((h) => clearTimeout(h as ReturnType<typeof setTimeout>));
  const random = opts.random ?? Math.random;
  const idOf = (m: RealtimeMessage): string | undefined => m.part_id ?? m.id;

  let status: RealtimeStatus = "connecting";
  let lastId: string | undefined = opts.initialLastId;
  let teardown: (() => void) | undefined;
  let reconnectTimer: unknown;
  let pollTimer: unknown;
  let polling = false;
  let attempt = 0;
  let stopped = false;

  // Bounded dedupe: a Set for O(1) membership + a queue to evict the oldest ids.
  const seen = new Set<string>();
  const order: string[] = [];

  function markSeen(id: string): boolean {
    if (seen.has(id)) return false;
    seen.add(id);
    order.push(id);
    if (order.length > DEDUPE_WINDOW) {
      const evicted = order.shift();
      if (evicted !== undefined) seen.delete(evicted);
    }
    return true;
  }

  function setStatus(next: RealtimeStatus): void {
    if (status === next) return;
    status = next;
    opts.onStatus?.(next);
  }

  function forward(msg: RealtimeMessage): void {
    const id = idOf(msg);
    if (id !== undefined) {
      if (!markSeen(id)) return; // duplicate (redelivery / poll+live overlap) — drop
      lastId = id;
    }
    opts.onEvent(msg);
  }

  async function pollTick(): Promise<void> {
    if (stopped || !polling) return;
    try {
      const messages = await opts.poll(lastId);
      for (const m of messages) forward(m);
    } catch {
      // A failed poll is non-fatal: the next tick retries. Nothing is acked/lost.
    }
    if (!stopped && polling) pollTimer = setTimer(() => void pollTick(), pollIntervalMs);
  }

  function startPolling(): void {
    if (polling) return;
    polling = true;
    setStatus("polling");
    void pollTick(); // poll immediately on downgrade, then on the interval
  }

  function stopPolling(): void {
    polling = false;
    if (pollTimer !== undefined) {
      clearTimer(pollTimer);
      pollTimer = undefined;
    }
  }

  function scheduleReconnect(): void {
    if (stopped || reconnectTimer !== undefined) return;
    const delay = jitteredBackoff(attempt, backoffBaseMs, backoffMaxMs, random);
    attempt += 1;
    reconnectTimer = setTimer(() => {
      reconnectTimer = undefined;
      goLive();
    }, delay);
  }

  function goLive(): void {
    if (stopped) return;
    if (teardown) {
      teardown();
      teardown = undefined;
    }
    if (status !== "polling") setStatus("connecting");
    try {
      teardown = opts.transport.subscribe(opts.channel, {
        onLive: () => {
          attempt = 0;
          stopPolling();
          setStatus("live");
        },
        onMessage: forward,
        onError: () => onLiveFailure(),
      });
    } catch {
      onLiveFailure();
    }
  }

  function onLiveFailure(): void {
    if (stopped) return;
    if (teardown) {
      teardown();
      teardown = undefined;
    }
    startPolling(); // downgrade immediately so no message is missed while reconnecting
    scheduleReconnect();
  }

  return {
    start(): void {
      stopped = false;
      goLive();
    },
    stop(): void {
      stopped = true;
      stopPolling();
      if (reconnectTimer !== undefined) {
        clearTimer(reconnectTimer);
        reconnectTimer = undefined;
      }
      if (teardown) {
        teardown();
        teardown = undefined;
      }
      setStatus("closed");
    },
    getStatus: () => status,
  };
}
