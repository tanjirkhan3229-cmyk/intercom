"use client";

import { AuthProvider } from "@/lib/auth";
import { QueryProvider } from "@/lib/query";

/** Client provider stack shared by the whole app: data cache + auth session. */
export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <QueryProvider>
      <AuthProvider>{children}</AuthProvider>
    </QueryProvider>
  );
}
