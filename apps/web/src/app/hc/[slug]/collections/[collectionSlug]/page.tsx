import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getPublicCollection } from "@/lib/public-api";

export const revalidate = 60;
export const dynamicParams = true;

export async function generateMetadata({
  params,
}: {
  params: Promise<{ slug: string; collectionSlug: string }>;
}): Promise<Metadata> {
  const { slug, collectionSlug } = await params;
  const collection = await getPublicCollection(slug, collectionSlug);
  if (!collection) return { title: "Collection" };
  return {
    title: collection.name,
    description: collection.description ?? `Articles in ${collection.name}.`,
  };
}

export default async function CollectionPage({
  params,
}: {
  params: Promise<{ slug: string; collectionSlug: string }>;
}) {
  const { slug, collectionSlug } = await params;
  const collection = await getPublicCollection(slug, collectionSlug);
  if (!collection) notFound();

  return (
    <div className="space-y-8">
      <nav aria-label="Breadcrumb" className="text-sm text-muted-foreground">
        <a href={`/hc/${slug}`} className="hover:underline">
          Help Center
        </a>
        <span aria-hidden> / </span>
        <span className="text-foreground">{collection.name}</span>
      </nav>

      <header className="space-y-2">
        <h1 className="text-3xl font-bold tracking-tight text-foreground">{collection.name}</h1>
        {collection.description ? (
          <p className="text-muted-foreground">{collection.description}</p>
        ) : null}
      </header>

      {collection.articles.length === 0 ? (
        <p className="text-muted-foreground">No published articles in this collection yet.</p>
      ) : (
        <ul className="divide-y divide-border rounded-lg border border-border bg-card">
          {collection.articles.map((article) => (
            <li key={article.id}>
              <a
                href={`/hc/${slug}/articles/${article.slug}`}
                className="block px-5 py-4 transition-colors hover:bg-muted/50"
              >
                <span className="block font-medium text-foreground">{article.title}</span>
                {article.excerpt ? (
                  <span className="mt-1 block text-sm text-muted-foreground">
                    {article.excerpt}
                  </span>
                ) : null}
              </a>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
