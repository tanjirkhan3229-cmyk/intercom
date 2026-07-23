"use client";

import { useRouter, useSearchParams } from "next/navigation";
import * as React from "react";
import { InboxShell } from "@/components/inbox/inbox-shell";
import { DEFAULT_VIEW } from "@/lib/views";
import { LoadingState } from "@/components/inbox/states";

/**
 * Inbox route. View + selected conversation live in the URL (`?view=you&c=cnv_…`) so a refresh
 * restores the exact working state (RFC P0.5 acceptance) and links are shareable.
 */
function Inbox() {
  const router = useRouter();
  const params = useSearchParams();
  const view = params.get("view") || DEFAULT_VIEW;
  const selected = params.get("c");

  const setUrl = React.useCallback(
    (next: { view?: string; c?: string | null }) => {
      const sp = new URLSearchParams(params.toString());
      if (next.view !== undefined) sp.set("view", next.view);
      if (next.c !== undefined) {
        if (next.c === null) sp.delete("c");
        else sp.set("c", next.c);
      }
      router.replace(`/app?${sp.toString()}`, { scroll: false });
    },
    [params, router],
  );

  return (
    <InboxShell
      view={view}
      onView={(v) => setUrl({ view: v, c: null })}
      selectedId={selected}
      onSelect={(id) => setUrl({ c: id })}
    />
  );
}

export default function AgentAppPage() {
  // useSearchParams needs a Suspense boundary (Next 15 App Router).
  return (
    <React.Suspense
      fallback={
        <div className="h-screen">
          <LoadingState />
        </div>
      }
    >
      <Inbox />
    </React.Suspense>
  );
}
