import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getPublicHelpCenter } from "@/lib/public-api";
import type { PublicCollectionSummary } from "@/lib/types";

export const revalidate = 60;
export const dynamicParams = true;

export async function generateMetadata({
  params,
}: {
  params: Promise<{ slug: string }>;
}): Promise<Metadata> {
  const { slug } = await params;
  const hc = await getPublicHelpCenter(slug);
  if (!hc) return { title: "Help Center" };
  return {
    title: hc.name,
    description: `${hc.name} — help center, guides, and answers to common questions.`,
  };
}

export default async function HelpCenterHome({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  const hc = await getPublicHelpCenter(slug);
  if (!hc) notFound();

  return (
    <div className="space-y-8">
      <h1 className="text-3xl font-bold tracking-tight text-foreground">{hc.name}</h1>

      {hc.collections.length === 0 ? (
        <p className="text-muted-foreground">
          There are no published collections yet. Please check back soon.
        </p>
      ) : (
        <nav aria-label="Collections">
          <ul className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            {hc.collections.map((collection) => (
              <li key={collection.slug}>
                <CollectionCard slug={slug} collection={collection} />
              </li>
            ))}
          </ul>
        </nav>
      )}
    </div>
  );
}

function CollectionCard({
  slug,
  collection,
}: {
  slug: string;
  collection: PublicCollectionSummary;
}) {
  return (
    <a
      href={`/hc/${slug}/collections/${collection.slug}`}
      className="flex h-full flex-col gap-2 rounded-lg border border-border bg-card p-5 transition-colors hover:border-[var(--hc-primary)]"
    >
      <div className="flex items-center gap-3">
        {collection.icon ? (
          <span aria-hidden className="text-2xl">
            {collection.icon}
          </span>
        ) : null}
        <h2 className="text-lg font-semibold text-foreground">{collection.name}</h2>
      </div>
      {collection.description ? (
        <p className="text-sm text-muted-foreground">{collection.description}</p>
      ) : null}
      <p className="mt-auto text-sm font-medium" style={{ color: "var(--hc-primary)" }}>
        {collection.article_count} {collection.article_count === 1 ? "article" : "articles"}
      </p>
    </a>
  );
}
