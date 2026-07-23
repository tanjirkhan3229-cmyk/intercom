import { notFound } from "next/navigation";
import { getPublicHelpCenter, searchPublic } from "@/lib/public-api";
import { SearchBox } from "@/components/hc-public/search-box";

// Search reflects the live query string, so render dynamically (no ISR caching).
export const dynamic = "force-dynamic";

export default async function SearchPage({
  params,
  searchParams,
}: {
  params: Promise<{ slug: string }>;
  searchParams: Promise<{ q?: string | string[] }>;
}) {
  const { slug } = await params;
  const { q: rawQ } = await searchParams;
  const q = (Array.isArray(rawQ) ? rawQ[0] : rawQ)?.trim() ?? "";

  // Confirm the tenant exists (and gets a branded layout / 404 on unknown slug).
  const hc = await getPublicHelpCenter(slug);
  if (!hc) notFound();

  const response = q ? await searchPublic(slug, q) : null;
  const results = response?.results ?? [];

  return (
    <div className="space-y-8">
      <div className="space-y-4">
        <h1 className="text-2xl font-bold tracking-tight text-foreground">Search</h1>
        <SearchBox slug={slug} initialQuery={q} />
      </div>

      {!q ? (
        <p className="text-muted-foreground">Type a query above to search the help center.</p>
      ) : results.length === 0 ? (
        <p className="text-muted-foreground">
          No results found for <span className="font-medium text-foreground">{q}</span>.
        </p>
      ) : (
        <div className="space-y-4">
          <p className="text-sm text-muted-foreground">
            {results.length} {results.length === 1 ? "result" : "results"} for{" "}
            <span className="font-medium text-foreground">{q}</span>
          </p>
          <ul className="divide-y divide-border rounded-lg border border-border bg-card">
            {results.map((result) => (
              <li key={result.slug}>
                <a
                  href={`/hc/${slug}/articles/${result.slug}`}
                  className="block px-5 py-4 transition-colors hover:bg-muted/50"
                >
                  <span className="block font-medium text-foreground">{result.title}</span>
                  {result.excerpt ? (
                    <span className="mt-1 block text-sm text-muted-foreground">
                      {result.excerpt}
                    </span>
                  ) : null}
                  {result.collection_slug ? (
                    <span
                      className="mt-1 block text-xs font-medium"
                      style={{ color: "var(--hc-primary)" }}
                    >
                      {result.collection_slug}
                    </span>
                  ) : null}
                </a>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
