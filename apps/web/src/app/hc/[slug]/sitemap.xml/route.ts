import { getPublicHelpCenter, listPublicArticles } from "@/lib/public-api";

// Time-based ISR for the sitemap too (matches the rest of the Help Center).
export const revalidate = 60;
export const dynamicParams = true;

function xmlEscape(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ slug: string }> },
) {
  const { slug } = await params;

  const [hc, articlePage] = await Promise.all([
    getPublicHelpCenter(slug),
    listPublicArticles(slug),
  ]);

  if (!hc) {
    return new Response("Not found", { status: 404 });
  }

  const base = process.env.HELP_CENTER_BASE_URL ?? "http://localhost:3000";
  const prefix = `${base}/hc/${slug}`;

  const urls: Array<{ loc: string; lastmod?: string }> = [{ loc: prefix }];

  for (const collection of hc.collections) {
    urls.push({ loc: `${prefix}/collections/${collection.slug}` });
  }

  for (const article of articlePage?.items ?? []) {
    urls.push({
      loc: `${prefix}/articles/${article.slug}`,
      lastmod: article.updated_at,
    });
  }

  const body = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${urls
  .map((u) => {
    const lastmod = u.lastmod ? `\n    <lastmod>${xmlEscape(u.lastmod)}</lastmod>` : "";
    return `  <url>\n    <loc>${xmlEscape(u.loc)}</loc>${lastmod}\n  </url>`;
  })
  .join("\n")}
</urlset>`;

  return new Response(body, {
    headers: { "Content-Type": "application/xml; charset=utf-8" },
  });
}
