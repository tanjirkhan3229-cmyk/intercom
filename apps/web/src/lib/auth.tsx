"use client";

/**
 * Auth layer (P0.1 identity). The access JWT lives only in memory; the rotating refresh token is
 * an httpOnly cookie the browser sends automatically (SDK sets `credentials: "include"`). On boot
 * we attempt a silent refresh to hydrate the session, then schedule a pre-expiry refresh. The
 * token-bound `RelayApi` is exposed via `useApi()` so every data hook talks to the API as the
 * authenticated principal (which drives `SET LOCAL app.ws` server-side — tenancy is sacred).
 */
import * as React from "react";
import { RelayApi, RelayApiError } from "./api";
import type { Session } from "./types";

interface AuthValue {
  status: "loading" | "authenticated" | "unauthenticated";
  session: Session | null;
  api: RelayApi;
  login: (email: string, password: string, workspaceId?: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = React.createContext<AuthValue | null>(null);

export function useAuth(): AuthValue {
  const ctx = React.useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within <AuthProvider>");
  return ctx;
}

/** The authenticated API client (token-bound). */
export function useApi(): RelayApi {
  return useAuth().api;
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = React.useState<AuthValue["status"]>("loading");
  const [session, setSession] = React.useState<Session | null>(null);
  const [token, setToken] = React.useState<string | undefined>(undefined);
  const refreshTimer = React.useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  // Rebuild the client whenever the token rotates.
  const api = React.useMemo(() => new RelayApi(token), [token]);
  // A stable unauthenticated client for refresh/login (uses the httpOnly cookie).
  const anon = React.useMemo(() => new RelayApi(), []);

  const scheduleRefresh = React.useCallback((expiresIn: number) => {
    if (refreshTimer.current) clearTimeout(refreshTimer.current);
    const delay = Math.max(5_000, (expiresIn - 60) * 1000);
    refreshTimer.current = setTimeout(() => void doRefresh(), delay);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const applyToken = React.useCallback(
    (t: { access_token: string; expires_in: number } & Session) => {
      setToken(t.access_token);
      setSession({ admin: t.admin, workspace: t.workspace, role: t.role });
      setStatus("authenticated");
      scheduleRefresh(t.expires_in);
    },
    [scheduleRefresh],
  );

  const doRefresh = React.useCallback(async () => {
    try {
      applyToken(await anon.refresh());
    } catch (err) {
      if (err instanceof RelayApiError && err.status === 401) {
        setToken(undefined);
        setSession(null);
        setStatus("unauthenticated");
      }
      // Non-401 (network/5xx): keep whatever session we have; the next timer retries.
    }
  }, [anon, applyToken]);

  React.useEffect(() => {
    void doRefresh();
    return () => {
      if (refreshTimer.current) clearTimeout(refreshTimer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const login = React.useCallback(
    async (email: string, password: string, workspaceId?: string) => {
      applyToken(await anon.login(email, password, workspaceId));
    },
    [anon, applyToken],
  );

  const logout = React.useCallback(async () => {
    try {
      await anon.logout();
    } finally {
      if (refreshTimer.current) clearTimeout(refreshTimer.current);
      setToken(undefined);
      setSession(null);
      setStatus("unauthenticated");
    }
  }, [anon]);

  const value = React.useMemo<AuthValue>(
    () => ({ status, session, api, login, logout }),
    [status, session, api, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
