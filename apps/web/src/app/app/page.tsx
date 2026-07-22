"use client";

import { useAuth } from "@/lib/auth";
import { Button } from "@/components/ui/button";

export default function AgentApp() {
  const { status } = useAuth();

  if (status === "loading") {
    return <ShellFrame>Loading…</ShellFrame>;
  }

  if (status === "unauthenticated") {
    return (
      <ShellFrame>
        <div className="flex flex-col items-center gap-4">
          <p className="text-muted-foreground">Sign in to access the inbox.</p>
          <Button disabled>Sign in (P0.1)</Button>
        </div>
      </ShellFrame>
    );
  }

  // Authenticated: the three-pane inbox (views / list / thread) ships in P0.5.
  return (
    <div className="grid h-screen grid-cols-[240px_360px_1fr] divide-x divide-border">
      <aside className="p-4 text-sm text-muted-foreground">Views</aside>
      <section className="p-4 text-sm text-muted-foreground">Conversations</section>
      <section className="p-4 text-sm text-muted-foreground">Thread</section>
    </div>
  );
}

function ShellFrame({ children }: { children: React.ReactNode }) {
  return (
    <main className="flex min-h-screen items-center justify-center px-6 text-center">
      <div>
        <h1 className="mb-6 text-2xl font-semibold">Relay Inbox</h1>
        {children}
      </div>
    </main>
  );
}
