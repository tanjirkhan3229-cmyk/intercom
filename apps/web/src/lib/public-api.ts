/**
 * Server-side fetchers for the PUBLIC Help Center (P0.8) — no auth, published content only.
 *
 * These run in Server Components / route handlers (never the browser), so they hit the API
 * with a plain `fetch` (no token). ISR: each response is cached with a 60s time-based revalidate
 * as the backstop; the `/api/revalidate` route does on-demand `revalidatePath` when an article is
 * published (driven by the backend outbox → `relay help-center-revalidate` consumer), so live
 * changes appear within seconds (acceptance #1). 404s return `null` so pages can call `notFound()`.
 */
import type { Page } from "@relay/shared";
import type {
  PublicArticle,
  PublicArticleSummary,
  PublicCollection,
  PublicHelpCenter,
  PublicSearchResponse,
} from "./types";

const BASE =
  process.env.API_BASE_URL ?? process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

/** Time-based ISR backstop (seconds); on-demand revalidation refreshes sooner on publish. */
export const HC_REVALIDATE_SECONDS = 60;

async function getJson<T>(path: string): Promise<T | null> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { accept: "application/json" },
    next: { revalidate: HC_REVALIDATE_SECONDS },
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`Help Center API ${res.status} for ${path}`);
  return (await res.json()) as T;
}

const seg = (s: string) => encodeURIComponent(s);

export function getPublicHelpCenter(slug: string): Promise<PublicHelpCenter | null> {
  return getJson<PublicHelpCenter>(`/v0/hc/${seg(slug)}`);
}

export function getPublicArticle(
  slug: string,
  articleSlug: string,
): Promise<PublicArticle | null> {
  return getJson<PublicArticle>(`/v0/hc/${seg(slug)}/articles/${seg(articleSlug)}`);
}

export function getPublicCollection(
  slug: string,
  collectionSlug: string,
): Promise<PublicCollection | null> {
  return getJson<PublicCollection>(`/v0/hc/${seg(slug)}/collections/${seg(collectionSlug)}`);
}

export function searchPublic(slug: string, q: string): Promise<PublicSearchResponse | null> {
  return getJson<PublicSearchResponse>(`/v0/hc/${seg(slug)}/search?q=${encodeURIComponent(q)}`);
}

export function listPublicArticles(
  slug: string,
): Promise<Page<PublicArticleSummary> | null> {
  return getJson<Page<PublicArticleSummary>>(`/v0/hc/${seg(slug)}/articles?limit=200`);
}
