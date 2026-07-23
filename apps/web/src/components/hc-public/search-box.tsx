"use client";

import { useState } from "react";
import { Input } from "@/components/ui/primitives";

/**
 * The only client component in the Help Center. A simple search form: on submit we
 * navigate to the tenant's `/hc/{slug}/search?q=...` page (which renders results
 * server-side). We use a full navigation via `window.location` to sidestep
 * `typedRoutes` friction on these dynamic content routes — keeping client JS minimal.
 */
export function SearchBox({ slug, initialQuery }: { slug: string; initialQuery?: string }) {
  const [q, setQ] = useState(initialQuery ?? "");

  function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const trimmed = q.trim();
    if (!trimmed) return;
    window.location.assign(`/hc/${slug}/search?q=${encodeURIComponent(trimmed)}`);
  }

  return (
    <form onSubmit={onSubmit} role="search" className="w-full max-w-xl">
      <label htmlFor="hc-search" className="sr-only">
        Search the help center
      </label>
      <Input
        id="hc-search"
        type="search"
        name="q"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Search for articles…"
        autoComplete="off"
        className="h-11 bg-background"
      />
    </form>
  );
}
