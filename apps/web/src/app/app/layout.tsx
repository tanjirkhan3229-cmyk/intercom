"use client";

import { useRouter } from "next/navigation";
import * as React from "react";
import { useAuth } from "@/lib/auth";
import { LoadingState } from "@/components/inbox/states";

// The agent app is client-rendered behind auth (RFC-001 §6.1: zero SSR on hot app paths).
// This layout is the gate: hydrate the session (silent refresh), then admit or bounce to /login.
export default function AppLayout({ children }: { children: React.ReactNode }) {
  const { status } = useAuth();
  const router = useRouter();

  React.useEffect(() => {
    if (status === "unauthenticated") router.replace("/login");
  }, [status, router]);

  if (status === "authenticated") return <>{children}</>;
  return (
    <div className="h-screen">
      <LoadingState label={status === "loading" ? "Signing you in…" : "Redirecting to sign in…"} />
    </div>
  );
}
