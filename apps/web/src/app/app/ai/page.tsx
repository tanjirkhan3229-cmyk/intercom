"use client";

import * as React from "react";
import Link from "next/link";
import { NekoSettings } from "@/components/ai/neko-settings";
import { NekoSandbox } from "@/components/ai/neko-sandbox";
import { NekoAnalytics } from "@/components/ai/neko-analytics";
import { RunInspector } from "@/components/ai/run-inspector";
import { cn } from "@/lib/utils";

type Tab = "settings" | "preview" | "analytics" | "runs";

const TAB_LABELS: Record<Tab, string> = {
  settings: "Settings",
  preview: "Preview sandbox",
  analytics: "Analytics",
  runs: "Run inspector",
};

/**
 * Neko (AI agent) workspace surface (RFC-003): settings + preview sandbox (P1.3), plus analytics
 * and the run inspector (P1.4). Mirrors the Help Center page shell (top bar with a back-link,
 * tabbed body); the app/app layout gates auth.
 */
export default function NekoPage() {
  const [tab, setTab] = React.useState<Tab>("settings");

  return (
    <div className="flex h-screen flex-col bg-background">
      <header className="flex items-center gap-3 border-b border-border px-4 py-3">
        <Link
          href="/app"
          className="text-xs font-medium text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
        >
          ← Inbox
        </Link>
        <h1 className="text-sm font-semibold">Neko — AI Agent</h1>
      </header>

      <div className="flex items-center gap-1 border-b border-border px-4 py-2">
        {(["settings", "preview", "analytics", "runs"] as const).map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTab(t)}
            aria-pressed={tab === t}
            className={cn(
              "rounded-md px-3 py-1 text-xs font-medium transition-colors",
              tab === t
                ? "bg-accent text-accent-foreground"
                : "text-muted-foreground hover:bg-accent/50",
            )}
          >
            {TAB_LABELS[t]}
          </button>
        ))}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-6">
        {tab === "settings" && <NekoSettings />}
        {tab === "preview" && (
          <div className="max-w-2xl">
            <NekoSandbox />
          </div>
        )}
        {tab === "analytics" && <NekoAnalytics />}
        {tab === "runs" && <RunInspector />}
      </div>
    </div>
  );
}
