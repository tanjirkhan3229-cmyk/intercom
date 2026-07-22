"use client";

import { AuthProvider } from "@/lib/auth";

// The agent app is client-rendered behind auth (RFC-001 §6.1: zero SSR on hot app paths).
export default function AppLayout({ children }: { children: React.ReactNode }) {
  return <AuthProvider>{children}</AuthProvider>;
}
