import Link from "next/link";
import { Button } from "@/components/ui/button";

// Marketing placeholder — statically generated (SSG). No dynamic data, no SSR.
export const dynamic = "force-static";

export default function MarketingHome() {
  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col items-center justify-center gap-8 px-6 text-center">
      <span className="rounded-full border border-border px-3 py-1 text-xs text-muted-foreground">
        Phase 0 · foundation
      </span>
      <h1 className="text-5xl font-semibold tracking-tight">Relay</h1>
      <p className="max-w-xl text-lg text-muted-foreground">
        Customer messaging, help center, and AI support on one conversation model. This is the
        marketing placeholder; the agent app lives behind auth.
      </p>
      <div className="flex gap-3">
        <Button asChild>
          <Link href="/app">Open the app</Link>
        </Button>
        <Button asChild variant="outline">
          <a href="https://github.com" target="_blank" rel="noreferrer">
            Docs
          </a>
        </Button>
      </div>
    </main>
  );
}
