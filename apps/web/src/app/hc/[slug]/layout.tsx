import type { ReactNode } from "react";
import { notFound } from "next/navigation";
import { getPublicHelpCenter } from "@/lib/public-api";
import { SearchBox } from "@/components/hc-public/search-box";

// ISR: 60s time-based backstop; on-demand revalidation (POST /api/revalidate) refreshes
// sooner on publish. New tenants/paths render on first request then cache.
export const revalidate = 60;
export const dynamicParams = true;

export default async function HelpCenterLayout({
  children,
  params,
}: {
  children: ReactNode;
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  const hc = await getPublicHelpCenter(slug);
  if (!hc) notFound();

  const primary = hc.primary_color ?? "#2563eb";
  const home = `/hc/${slug}`;

  return (
    <div
      className="flex min-h-screen flex-col bg-background text-foreground"
      style={{ ["--hc-primary" as string]: primary } as React.CSSProperties}
    >
      <header className="text-white shadow-sm" style={{ backgroundColor: "var(--hc-primary)" }}>
        <div className="mx-auto flex w-full max-w-4xl flex-col gap-4 px-4 py-6 sm:px-6">
          <a href={home} className="flex items-center gap-3 self-start" aria-label={hc.name}>
            {hc.logo_url ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={hc.logo_url}
                alt={hc.name}
                className="h-8 w-auto max-w-[200px] object-contain"
              />
            ) : (
              <span className="text-lg font-semibold text-white">{hc.name}</span>
            )}
          </a>
          <SearchBox slug={slug} />
        </div>
      </header>

      <main className="mx-auto w-full max-w-4xl flex-1 px-4 py-10 sm:px-6">{children}</main>

      <footer className="border-t border-border">
        <div className="mx-auto w-full max-w-4xl px-4 py-6 text-sm text-muted-foreground sm:px-6">
          Powered by Relay
        </div>
      </footer>
    </div>
  );
}
