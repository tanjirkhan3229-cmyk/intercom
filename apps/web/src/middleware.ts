import { NextResponse, type NextRequest } from "next/server";

/**
 * Subdomain → path rewrite for hosted Help Centers.
 *
 * In production each workspace is served at `{workspace-slug}.relayhc.com`; this middleware
 * maps that host onto the internal `/hc/{slug}` App Router tree so the rest of the app never
 * has to know about subdomains.
 *
 * SAFE BY DEFAULT: the whole thing is gated on the `HELP_CENTER_HOST_SUFFIX` env var. When it
 * is unset (e.g. local dev, or the agent app deployment) this middleware is a pure pass-through
 * and will NEVER rewrite anything — so it cannot interfere with `/app`, `/login`, `/api`, etc.
 * Only when the suffix is configured AND the incoming host actually ends with it do we rewrite.
 */
export function middleware(req: NextRequest): NextResponse {
  const suffix = process.env.HELP_CENTER_HOST_SUFFIX;

  // No suffix configured → do nothing at all.
  if (!suffix) return NextResponse.next();

  const host = (req.headers.get("host") ?? "").split(":")[0]?.toLowerCase() ?? "";
  const normalizedSuffix = suffix.toLowerCase();

  // Host must end with the configured suffix (e.g. ".relayhc.com").
  if (!host.endsWith(normalizedSuffix)) return NextResponse.next();

  // The leading label before the suffix is the workspace slug.
  const slug = host.slice(0, host.length - normalizedSuffix.length);
  if (!slug || slug.includes(".")) return NextResponse.next();

  const { pathname } = req.nextUrl;

  // Never re-rewrite internal / already-scoped routes.
  if (
    pathname.startsWith("/hc") ||
    pathname.startsWith("/app") ||
    pathname.startsWith("/login") ||
    pathname.startsWith("/api") ||
    pathname.startsWith("/_next")
  ) {
    return NextResponse.next();
  }

  return NextResponse.rewrite(new URL(`/hc/${slug}${pathname}`, req.url));
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
