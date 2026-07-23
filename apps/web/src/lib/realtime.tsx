"use client";

/**
 * Realtime wiring for the agent app. We reuse the transport-agnostic controller shipped in P0.4
 * (`@relay/shared` `createRealtimeChannel`) — it owns auto-downgrade to long-poll, jittered
 * reconnect, and dedupe-by-part-id — and inject a Centrifugo (`centrifuge`) live transport plus a
 * `poll` fallback. Realtime is a *signal*: on any event we invalidate the relevant TanStack query
 * so the cache re-reads from Postgres (the source of truth), which is why a gateway outage never
 * loses a message (RFC-001 §6.3).
 */
import { Centrifuge } from "centrifuge";
import {
  createRealtimeChannel,
  type LiveHandlers,
  type LiveTransport,
  type RealtimeMessage,
  type RealtimeStatus,
} from "@relay/shared";
import * as React from "react";
import { useApi } from "./auth";
import type { RelayApi } from "./api";

/** Build a Centrifugo-backed live transport bound to one already-resolved websocket URL. */
function centrifugoTransport(api: RelayApi, wsUrl: string): LiveTransport {
  return {
    subscribe(channel: string, handlers: LiveHandlers): () => void {
      const centrifuge = new Centrifuge(wsUrl, {
        // getToken lets Centrifugo refresh an expiring connection JWT without a reconnect.
        getToken: async () => (await api.realtimeToken()).token,
      });
      const sub = centrifuge.newSubscription(channel, {
        getToken: async () => {
          const res = await api.realtimeSubscribe([channel]);
          const token = res.tokens[channel];
          if (!token) throw new Error(`no subscription token for ${channel}`);
          return token;
        },
      });
      sub.on("publication", (ctx) => handlers.onMessage(ctx.data as RealtimeMessage));
      sub.on("subscribed", () => handlers.onLive());
      sub.on("error", (e) => handlers.onError(e));
      centrifuge.on("error", (e) => handlers.onError(e));
      centrifuge.on("disconnected", (e) => handlers.onError(e));
      sub.subscribe();
      centrifuge.connect();
      return () => {
        sub.unsubscribe();
        centrifuge.disconnect();
      };
    },
  };
}

export interface UseRealtimeChannelOptions {
  /** `conv:{id}` or `inbox:{ws}:{bucket}` — `null` disables the subscription. */
  channel: string | null;
  /** Fallback poll: return messages newer than `after`, ascending. Empty array = nothing new. */
  poll: (after: string | undefined) => Promise<RealtimeMessage[]>;
  /** Called per fresh (deduped) event from either transport. */
  onEvent: (msg: RealtimeMessage) => void;
  onStatus?: (status: RealtimeStatus) => void;
  initialLastId?: string;
}

/**
 * Subscribe to one realtime channel for the lifetime of the calling component (or until `channel`
 * changes). Both web and widget clients auto-downgrade to polling on websocket failure.
 */
export function useRealtimeChannel(opts: UseRealtimeChannelOptions): void {
  const api = useApi();
  const { channel, initialLastId } = opts;

  // Keep the latest callbacks in refs so changing their identity per render doesn't tear down
  // and rebuild the (relatively expensive) websocket subscription.
  const pollRef = React.useRef(opts.poll);
  const onEventRef = React.useRef(opts.onEvent);
  const onStatusRef = React.useRef(opts.onStatus);
  pollRef.current = opts.poll;
  onEventRef.current = opts.onEvent;
  onStatusRef.current = opts.onStatus;

  React.useEffect(() => {
    if (!channel) return;
    let disposed = false;
    let stop: (() => void) | undefined;

    void (async () => {
      let wsUrl: string;
      try {
        wsUrl = (await api.realtimeToken()).ws_url;
      } catch {
        // Can't even mint a connection token — fall back to a pure poll loop so the pane still
        // updates (degraded, but never blank). The controller handles the retry cadence.
        wsUrl = "";
      }
      if (disposed) return;

      // No connection token/URL → signal failure so the controller downgrades to its poll loop
      // (a silent no-op transport would strand it in "connecting" and never poll).
      const transport: LiveTransport = wsUrl
        ? centrifugoTransport(api, wsUrl)
        : {
            subscribe: (_channel, handlers) => {
              handlers.onError(new Error("realtime connection token unavailable"));
              return () => {};
            },
          };

      const ctl = createRealtimeChannel({
        channel,
        transport,
        poll: (after) => pollRef.current(after),
        onEvent: (msg) => onEventRef.current(msg),
        onStatus: (s) => onStatusRef.current?.(s),
        initialLastId,
      });
      ctl.start();
      stop = () => ctl.stop();
    })();

    return () => {
      disposed = true;
      stop?.();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [channel, api]);
}
