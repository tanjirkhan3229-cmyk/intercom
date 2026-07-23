"use client";

/**
 * Glue between the realtime channel controller and the TanStack cache. Events are treated as
 * signals: on each one we invalidate the affected query so it re-reads from Postgres (the source
 * of truth). This keeps the live path, the poll fallback, and outbox at-least-once redeliveries
 * all converging on one correct render (RFC-001 §6.3).
 */
import { useQueryClient, type InfiniteData } from "@tanstack/react-query";
import type { Page } from "@relay/shared";
import * as React from "react";
import { useApi, useAuth } from "./auth";
import { useRealtimeChannel } from "./realtime";
import { qk } from "./keys";
import { inboxBucketForView } from "./views";
import type { Part } from "./types";

/** Subscribe to the selected conversation's thread channel; keep its parts + head fresh. */
export function useThreadRealtime(conversationId: string | null): void {
  const api = useApi();
  const qc = useQueryClient();

  const initialLastId = React.useMemo(() => {
    if (!conversationId) return undefined;
    const data = qc.getQueryData<InfiniteData<Page<Part>>>(qk.parts(conversationId));
    return data?.pages[0]?.items[0]?.id; // newest-first → [0][0] is the newest part held
  }, [qc, conversationId]);

  useRealtimeChannel({
    channel: conversationId ? `conv:${conversationId}` : null,
    poll: async (after) => {
      if (!conversationId) return [];
      const page = await api.listPartsAfter(conversationId, after);
      // The controller only needs an id per message to dedupe + advance its cursor; the actual
      // parts are re-read from cache on invalidate, so we forward id-only signals.
      return page.items.map((p) => ({ id: p.id }));
    },
    onEvent: () => {
      if (!conversationId) return;
      void qc.invalidateQueries({ queryKey: qk.parts(conversationId) });
      void qc.invalidateQueries({ queryKey: qk.conversation(conversationId) });
      void qc.invalidateQueries({ queryKey: qk.conversationsRoot });
    },
    initialLastId,
  });
}

/** Subscribe to the inbox bucket for the current view; refresh conversation lists on activity. */
export function useInboxRealtime(viewId: string): void {
  const { session } = useAuth();
  const qc = useQueryClient();
  const ws = session?.workspace.id;
  const channel = ws ? `inbox:${ws}:${inboxBucketForView(viewId)}` : null;

  const refresh = () => void qc.invalidateQueries({ queryKey: qk.conversationsRoot });

  useRealtimeChannel({
    channel,
    // No append-stream to replay for an inbox view; the fallback simply re-lists on each tick.
    poll: async () => {
      refresh();
      return [];
    },
    onEvent: refresh,
  });
}

/** Best-effort presence heartbeat so the queue monitor (P0.9) can show who's online. */
export function usePresenceHeartbeat(intervalMs = 20_000): void {
  const api = useApi();
  const { status } = useAuth();
  React.useEffect(() => {
    if (status !== "authenticated") return;
    let stopped = false;
    const beat = () => {
      if (stopped) return;
      void api.presence().catch(() => {});
    };
    beat();
    const t = setInterval(beat, intervalMs);
    return () => {
      stopped = true;
      clearInterval(t);
    };
  }, [api, status, intervalMs]);
}
