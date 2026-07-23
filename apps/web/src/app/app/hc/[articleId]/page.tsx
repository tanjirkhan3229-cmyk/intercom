"use client";

import type { Route } from "next";
import Link from "next/link";
import { useParams } from "next/navigation";
import { ArticleEditor } from "@/components/hc/article-editor";
import { ErrorState } from "@/components/inbox/states";

/** Single-article editor route (RFC P0.8). `articleId` comes from the dynamic segment. */
export default function ArticleEditorPage() {
  const params = useParams<{ articleId: string }>();
  const articleId = params?.articleId;

  return (
    <div className="flex h-screen flex-col bg-background">
      <header className="flex items-center gap-3 border-b border-border px-4 py-3">
        <Link
          href={"/app/hc" as Route}
          className="text-xs font-medium text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
        >
          ← Help Center
        </Link>
      </header>
      <div className="min-h-0 flex-1">
        {articleId ? (
          <ArticleEditor articleId={articleId} />
        ) : (
          <ErrorState title="Missing article id" />
        )}
      </div>
    </div>
  );
}
