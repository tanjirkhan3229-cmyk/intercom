"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as React from "react";

/**
 * One QueryClient per browser session. Defaults tuned for an inbox where realtime is the primary
 * freshness mechanism (subscriptions invalidate the cache) — so background refetch is conservative
 * and retries are bounded (RFC master rule: bounded retries).
 */
function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 15_000,
        gcTime: 5 * 60_000,
        retry: 2,
        refetchOnWindowFocus: false,
      },
      mutations: { retry: 0 },
    },
  });
}

let browserClient: QueryClient | undefined;
function getQueryClient(): QueryClient {
  // Reuse a single client in the browser; make a fresh one per request on the server.
  if (typeof window === "undefined") return makeQueryClient();
  browserClient ??= makeQueryClient();
  return browserClient;
}

export function QueryProvider({ children }: { children: React.ReactNode }) {
  const [client] = React.useState(getQueryClient);
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
