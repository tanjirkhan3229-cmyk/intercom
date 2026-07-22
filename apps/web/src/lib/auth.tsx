"use client";

/**
 * Client-side auth gate placeholder. The real flow (access JWT in memory + httpOnly
 * rotating refresh cookie) lands in P0.1; for now this only models the loading/gated
 * states so the agent app shell can be CSR "behind auth".
 */

import * as React from "react";

interface AuthState {
  status: "loading" | "authenticated" | "unauthenticated";
  admin: { id: string; name: string } | null;
}

const AuthContext = React.createContext<AuthState>({ status: "loading", admin: null });

export function useAuth(): AuthState {
  return React.useContext(AuthContext);
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = React.useState<AuthState>({ status: "loading", admin: null });

  React.useEffect(() => {
    // Placeholder: P0.1 will call the refresh endpoint to hydrate the session.
    setState({ status: "unauthenticated", admin: null });
  }, []);

  return <AuthContext.Provider value={state}>{children}</AuthContext.Provider>;
}
