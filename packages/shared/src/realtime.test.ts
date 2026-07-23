/**
 * Self-check for the realtime fallback controller (RFC-001 §6.3 acceptance: auto-downgrade on ws
 * failure, no message loss, dedupe, jittered reconnect). No framework, no browser, no Node types —
 * a fake transport, a fake poll, and an injected clock. It typechecks under the package tsconfig
 * (ES2022 + DOM) and runs under any TS runner (tsx / Node ≥ 22 --experimental-strip-types). The
 * full "two browser sessions" e2e lives in P0.5/P0.6.
 */
import {
  createRealtimeChannel,
  jitteredBackoff,
  type LiveHandlers,
  type RealtimeMessage,
} from "./realtime.js";

function assert(condition: boolean, message: string): void {
  if (!condition) throw new Error(`assertion failed: ${message}`);
}

interface FakeTimer {
  id: number;
  fn: () => void;
  ms: number;
}

function fakeClock() {
  let seq = 0;
  let timers: FakeTimer[] = [];
  return {
    setTimer: (fn: () => void, ms: number): number => {
      const id = ++seq;
      timers.push({ id, fn, ms });
      return id;
    },
    clearTimer: (h: unknown): void => {
      timers = timers.filter((t) => t.id !== h);
    },
    pending: (): FakeTimer[] => timers.slice(),
    fireAll: (): void => {
      const batch = timers;
      timers = [];
      for (const t of batch) t.fn();
    },
  };
}

// Yield to the microtask queue so an awaited poll() resolves — no Node globals needed.
const flush = (): Promise<void> => Promise.resolve().then(() => Promise.resolve());

async function main(): Promise<void> {
  // --- Jittered backoff bounds (full jitter). ---
  assert(jitteredBackoff(0, 1000, 30000, () => 0) === 500, "backoff attempt0 min");
  assert(jitteredBackoff(0, 1000, 30000, () => 1) === 1000, "backoff attempt0 max");
  assert(jitteredBackoff(3, 1000, 30000, () => 0) === 4000, "backoff attempt3 min (ceil 8000)");
  assert(jitteredBackoff(10, 1000, 30000, () => 1) === 30000, "backoff clamped to max");

  // --- Controller: live → downgrade → poll (no loss + dedupe) → jittered reconnect. ---
  const events: RealtimeMessage[] = [];
  let handlers: LiveHandlers | null = null;
  let subscribeCount = 0;
  const pollQueue: RealtimeMessage[][] = [];
  const clock = fakeClock();

  const channel = createRealtimeChannel({
    channel: "conv:cnv_x",
    transport: {
      subscribe: (_channel, h) => {
        subscribeCount += 1;
        handlers = h;
        return () => {};
      },
    },
    poll: () => Promise.resolve(pollQueue.shift() ?? []),
    onEvent: (m) => events.push(m),
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
    random: () => 0,
    pollIntervalMs: 100,
    backoffBaseMs: 1000,
    backoffMaxMs: 30000,
  });

  channel.start();
  assert(subscribeCount === 1, "subscribed on start");
  handlers!.onLive();
  assert(channel.getStatus() === "live", "live after onLive");

  // A live part + two typing signals (no id → never deduped, always forwarded).
  handlers!.onMessage({ topic: "conversation.part.created", part_id: "msg_1" });
  handlers!.onMessage({ topic: "typing", actor_id: "adm_a" });
  handlers!.onMessage({ topic: "typing", actor_id: "adm_a" });
  assert(events.length === 3, "3 events forwarded live");

  // Websocket dies. The next poll holds the just-seen msg_1 (dup) + a msg_2 the fanout never
  // pushed during the outage — msg_2 must survive (no loss), msg_1 must be deduped.
  pollQueue.push([{ id: "msg_1" }, { id: "msg_2" }]);
  handlers!.onError(new Error("ws down"));
  assert(channel.getStatus() === "polling", "downgraded to polling");
  await flush();
  assert(events.length === 4, "poll added exactly the new msg_2");
  assert(events[events.length - 1]!.id === "msg_2", "msg_2 forwarded, msg_1 deduped");

  // A reconnect was scheduled with jittered backoff (attempt 0, random 0 → 500ms).
  assert(
    clock.pending().some((t) => t.ms === 500),
    "reconnect scheduled with jittered backoff",
  );

  // Fire timers → reconnect re-subscribes; onLive returns to live and stops polling.
  clock.fireAll();
  assert(subscribeCount === 2, "reconnected (re-subscribed)");
  handlers!.onLive();
  assert(channel.getStatus() === "live", "back to live after reconnect");

  channel.stop();
  assert(channel.getStatus() === "closed", "closed on stop");

  // eslint-disable-next-line no-console
  console.log("realtime.test OK");
}

void main();
