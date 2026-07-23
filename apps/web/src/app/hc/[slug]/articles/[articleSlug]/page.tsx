import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getPublicArticle } from "@/lib/public-api";
import { BlockRender } from "@/components/hc-public/block-render";

export const revalidate = 60;
export const dynamicParams = true;

export async function generateMetadata({
  params,
}: {
  params: Promise<{ slug: string; articleSlug: string }>;
}): Promise<Metadata> {
  const { slug, articleSlug } = await params;
  const article = await getPublicArticle(slug, articleSlug);
  if (!article) return { title: "Article" };
  const title = article.seo_title ?? article.title;
  const description = article.seo_description ?? undefined;
  return {
    title,
    description,
    openGraph: { title, description, type: "article" },
  };
}

function formatDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-US", { year: "numeric", month: "long", day: "numeric" });
}

export default async function ArticlePage({
  params,
}: {
  params: Promise<{ slug: string; articleSlug: string }>;
}) {
  const { slug, articleSlug } = await params;
  const article = await getPublicArticle(slug, articleSlug);
  if (!article) notFound();

  const blocks = article.body?.blocks ?? [];

  return (
    <article className="space-y-6">
      <nav aria-label="Breadcrumb" className="text-sm text-muted-foreground">
        <a href={`/hc/${slug}`} className="hover:underline">
          Help Center
        </a>
        {article.collection_slug ? (
          <>
            <span aria-hidden> / </span>
            <a
              href={`/hc/${slug}/collections/${article.collection_slug}`}
              className="hover:underline"
            >
              {article.collection_slug}
            </a>
          </>
        ) : null}
      </nav>

      <header className="space-y-2 border-b border-border pb-6">
        <h1 className="text-3xl font-bold tracking-tight text-foreground">{article.title}</h1>
        <p className="text-sm text-muted-foreground">
          Last updated{" "}
          <time dateTime={article.updated_at}>{formatDate(article.updated_at)}</time>
        </p>
      </header>

      <BlockRender blocks={blocks} />
    </article>
  );
}
