"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import type { Route } from "next";
import { useArticles, flattenArticles, useCreateArticle } from "@/lib/hc-hooks";
import { timeAgo } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Badge, Spinner } from "@/components/ui/primitives";
import { LoadingState, EmptyState, ErrorState } from "@/components/inbox/states";
import { cn } from "@/lib/utils";
import { CollectionsPanel } from "@/components/hc/collections-panel";
import { HelpCenterSettings } from "@/components/hc/help-center-settings";
import { SourcesPanel } from "@/components/hc/sources-panel";
import type { ArticleStatus, ArticleSummary } from "@/lib/types";

type Filter = "all" | "draft" | "published";

/**
 * Help Center management page (RFC P0.8). Left rail: collections + settings. Main column: the
 * article list with a status filter and a "New article" action that creates an untitled draft and
 * jumps straight into the editor.
 */
export default function HelpCenterPage() {
  const router = useRouter();
  const createArticle = useCreateArticle();
  const [filter, setFilter] = React.useState<Filter>("all");

  const articles = useArticles(filter === "all" ? {} : { status: filter });
  const items = React.useMemo(
    () => flattenArticles(articles.data?.pages),
    [articles.data],
  );

  const onNew = async () => {
    const created = await createArticle.mutateAsync({ title: "Untitled" });
    router.push(`/app/hc/${created.id}` as Route);
  };

  return (
    <div className="flex h-screen flex-col bg-background">
      {/* Top bar */}
      <header className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
        <div className="flex items-center gap-3">
          <Link
            href="/app"
            className="text-xs font-medium text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
          >
            ← Inbox
          </Link>
          <h1 className="text-sm font-semibold">Help Center</h1>
        </div>
        <Button size="sm" onClick={() => void onNew()} disabled={createArticle.isPending}>
          {createArticle.isPending ? <Spinner className="h-3.5 w-3.5" /> : "New article"}
        </Button>
      </header>

      <div className="flex min-h-0 flex-1">
        {/* Left rail */}
        <aside className="w-72 shrink-0 overflow-y-auto border-r border-border p-4">
          <div className="flex flex-col gap-6">
            <CollectionsPanel />
            <div className="border-t border-border pt-6">
              <SourcesPanel />
            </div>
            <div className="border-t border-border pt-6">
              <HelpCenterSettings />
            </div>
          </div>
        </aside>

        {/* Main: articles */}
        <main className="flex min-w-0 flex-1 flex-col">
          <div className="flex items-center gap-1 border-b border-border px-4 py-2">
            {(["all", "draft", "published"] as const).map((f) => (
              <button
                key={f}
                type="button"
                onClick={() => setFilter(f)}
                aria-pressed={filter === f}
                className={cn(
                  "rounded-md px-3 py-1 text-xs font-medium capitalize transition-colors",
                  filter === f
                    ? "bg-accent text-accent-foreground"
                    : "text-muted-foreground hover:bg-accent/50",
                )}
              >
                {f}
              </button>
            ))}
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto">
            {articles.isLoading ? (
              <LoadingState label="Loading articles…" />
            ) : articles.isError ? (
              <ErrorState error={articles.error} onRetry={() => void articles.refetch()} />
            ) : items.length === 0 ? (
              <EmptyState
                title="No articles yet"
                hint='Click "New article" to write your first help doc.'
              />
            ) : (
              <>
                <ul className="divide-y divide-border">
                  {items.map((a) => (
                    <ArticleRow key={a.id} article={a} />
                  ))}
                </ul>
                {articles.hasNextPage && (
                  <div className="p-3 text-center">
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={articles.isFetchingNextPage}
                      onClick={() => void articles.fetchNextPage()}
                    >
                      {articles.isFetchingNextPage ? <Spinner className="h-3.5 w-3.5" /> : "Load more"}
                    </Button>
                  </div>
                )}
              </>
            )}
          </div>
        </main>
      </div>
    </div>
  );
}

function statusVariant(status: ArticleStatus): "default" | "muted" {
  return status === "published" ? "default" : "muted";
}

function ArticleRow({ article }: { article: ArticleSummary }) {
  return (
    <li>
      <Link
        href={`/app/hc/${article.id}` as Route}
        className="flex items-center gap-3 px-4 py-3 transition-colors hover:bg-accent/50"
      >
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium">{article.title || "Untitled"}</p>
          <p className="mt-0.5 truncate text-xs text-muted-foreground">/{article.slug}</p>
        </div>
        <span className="shrink-0 text-[11px] text-muted-foreground">
          {timeAgo(article.updated_at)}
        </span>
        <Badge variant={statusVariant(article.status)} className="shrink-0 capitalize">
          {article.status}
        </Badge>
      </Link>
    </li>
  );
}
