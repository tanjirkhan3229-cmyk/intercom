"use client";

import * as React from "react";
import { useAuth } from "@/lib/auth";
import {
  useArticle,
  useArticleStatusMutations,
  useCollections,
  useUpdateArticle,
} from "@/lib/hc-hooks";
import { Button } from "@/components/ui/button";
import { Input, Textarea, Badge, Spinner } from "@/components/ui/primitives";
import { LoadingState, ErrorState } from "@/components/inbox/states";
import type { ArticleInput, DocBlock } from "@/lib/types";
import { BlockEditor } from "./block-editor";

const AUTOSAVE_MS = 800;

/**
 * Full article editor (RFC P0.8). Local draft state is the source of truth while editing; every
 * change schedules a debounced autosave through `useUpdateArticle`. Publish/unpublish/delete run
 * through `useArticleStatusMutations` (which also fires ISR revalidation server-side).
 */
export function ArticleEditor({ articleId }: { articleId: string }) {
  const query = useArticle(articleId);
  const collections = useCollections();
  const update = useUpdateArticle(articleId);
  const status = useArticleStatusMutations(articleId);
  const { session } = useAuth();

  // Local, editable form state, hydrated once from the server article.
  const [title, setTitle] = React.useState("");
  const [slug, setSlug] = React.useState("");
  const [collectionId, setCollectionId] = React.useState<string | null>(null);
  const [seoTitle, setSeoTitle] = React.useState("");
  const [seoDescription, setSeoDescription] = React.useState("");
  const [blocks, setBlocks] = React.useState<DocBlock[]>([]);
  const hydratedFor = React.useRef<string | null>(null);
  const dirty = React.useRef(false);
  const timer = React.useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const article = query.data;

  // Hydrate once per article id (don't clobber edits on background refetches).
  React.useEffect(() => {
    if (!article || hydratedFor.current === article.id) return;
    hydratedFor.current = article.id;
    setTitle(article.title ?? "");
    setSlug(article.slug ?? "");
    setCollectionId(article.collection_id);
    setSeoTitle(article.seo_title ?? "");
    setSeoDescription(article.seo_description ?? "");
    setBlocks(article.body?.blocks ?? []);
  }, [article]);

  // The latest field values, mirrored into a ref so the debounced / flushed save always reads
  // *current* state. A save() closing over state would persist the value from before the edit
  // that armed the timer (off-by-one keystroke). `unsaved` drives an honest save indicator.
  const [unsaved, setUnsaved] = React.useState(false);
  const latest = React.useRef({ title, slug, collectionId, seoTitle, seoDescription, blocks });
  latest.current = { title, slug, collectionId, seoTitle, seoDescription, blocks };

  // Depend on `mutateAsync` (React Query keeps it referentially stable), NOT the whole `update`
  // result object — that is recreated every render, which would give `save` a new identity every
  // render and make the [save]-keyed flush effect below run its cleanup on every keystroke.
  const { mutateAsync } = update;
  const save = React.useCallback(async () => {
    const s = latest.current;
    const input: ArticleInput = {
      title: s.title,
      slug: s.slug || undefined,
      collection_id: s.collectionId,
      body: { blocks: s.blocks },
      seo_title: s.seoTitle || null,
      seo_description: s.seoDescription || null,
    };
    try {
      await mutateAsync(input);
      dirty.current = false;
      setUnsaved(false);
    } catch {
      // Keep dirty so the indicator surfaces the failure and the next edit/flush retries.
    }
  }, [mutateAsync]);

  // Debounced autosave: any field change marks dirty and (re)arms the timer.
  const scheduleSave = React.useCallback(() => {
    dirty.current = true;
    setUnsaved(true);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => void save(), AUTOSAVE_MS);
  }, [save]);

  React.useEffect(() => {
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, []);

  // Flush a pending save on unmount / navigation so nothing is lost.
  React.useEffect(() => {
    return () => {
      if (dirty.current) void save();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [save]);

  if (query.isLoading) return <LoadingState label="Loading article…" />;
  if (query.isError) {
    return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  }
  if (!article) return <ErrorState title="Article not found" />;

  const published = article.status === "published";
  const workspaceSlug = session?.workspace.slug;
  const liveHref =
    published && workspaceSlug ? `/hc/${workspaceSlug}/articles/${article.slug}` : null;

  const onField =
    <T,>(setter: (v: T) => void) =>
    (v: T) => {
      setter(v);
      scheduleSave();
    };

  return (
    <div className="mx-auto flex h-full max-w-3xl flex-col gap-5 overflow-y-auto p-6" data-testid="article-editor">
      {/* Header: status + save indicator + actions */}
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <Badge variant={published ? "default" : "muted"}>{article.status}</Badge>
          <SaveIndicator
            saving={update.isPending}
            error={update.isError}
            saved={update.isSuccess}
            unsaved={unsaved}
          />
        </div>
        <div className="flex items-center gap-2">
          {liveHref && (
            <a
              href={liveHref}
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs font-medium text-primary underline-offset-4 hover:underline"
            >
              View live ↗
            </a>
          )}
          {published ? (
            <Button
              variant="outline"
              size="sm"
              disabled={status.unpublish.isPending}
              onClick={() => status.unpublish.mutate()}
            >
              {status.unpublish.isPending ? <Spinner className="h-3.5 w-3.5" /> : "Unpublish"}
            </Button>
          ) : (
            <Button
              size="sm"
              disabled={status.publish.isPending}
              onClick={async () => {
                // Flush any pending edit and wait for it to land BEFORE publishing, so the
                // published snapshot always reflects the latest content (no PATCH/POST race).
                if (timer.current) clearTimeout(timer.current);
                if (dirty.current) await save();
                status.publish.mutate();
              }}
            >
              {status.publish.isPending ? <Spinner className="h-3.5 w-3.5" /> : "Publish"}
            </Button>
          )}
          <DeleteButton
            onConfirm={() => {
              // Cancel any pending autosave so we don't PATCH (then 404) an article we're deleting.
              if (timer.current) clearTimeout(timer.current);
              dirty.current = false;
              status.remove.mutate();
            }}
            pending={status.remove.isPending}
          />
        </div>
      </div>

      {/* Title */}
      <div className="flex flex-col gap-1">
        <Field label="Title" />
        <Input
          value={title}
          onChange={(e) => onField(setTitle)(e.target.value)}
          placeholder="Article title"
          className="h-11 text-lg font-semibold"
        />
      </div>

      {/* Slug + collection */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div className="flex flex-col gap-1">
          <Field label="Slug" />
          <Input
            value={slug}
            onChange={(e) => onField(setSlug)(e.target.value)}
            placeholder="url-slug"
          />
        </div>
        <div className="flex flex-col gap-1">
          <Field label="Collection" />
          <select
            value={collectionId ?? ""}
            onChange={(e) => onField(setCollectionId)(e.target.value || null)}
            className="flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
          >
            <option value="">No collection</option>
            {collections.data?.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* Body */}
      <div className="flex flex-col gap-2 border-t border-border pt-4">
        <Field label="Body" />
        <BlockEditor value={blocks} onChange={onField(setBlocks)} />
      </div>

      {/* SEO */}
      <div className="flex flex-col gap-4 border-t border-border pt-4">
        <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          SEO
        </p>
        <div className="flex flex-col gap-1">
          <Field label="SEO title" />
          <Input
            value={seoTitle}
            onChange={(e) => onField(setSeoTitle)(e.target.value)}
            placeholder="Defaults to the article title"
          />
        </div>
        <div className="flex flex-col gap-1">
          <Field label="SEO description" />
          <Textarea
            value={seoDescription}
            onChange={(e) => onField(setSeoDescription)(e.target.value)}
            placeholder="A short summary for search engines"
          />
        </div>
      </div>
    </div>
  );
}

function Field({ label }: { label: string }) {
  return (
    <label className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
      {label}
    </label>
  );
}

function SaveIndicator({
  saving,
  error,
  saved,
  unsaved,
}: {
  saving: boolean;
  error: boolean;
  saved: boolean;
  unsaved: boolean;
}) {
  if (saving) {
    return (
      <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <Spinner className="h-3 w-3" /> Saving…
      </span>
    );
  }
  if (error) return <span className="text-xs text-destructive">Save failed</span>;
  if (unsaved) return <span className="text-xs text-muted-foreground">Unsaved changes</span>;
  if (saved) return <span className="text-xs text-muted-foreground">Saved</span>;
  return null;
}

function DeleteButton({ onConfirm, pending }: { onConfirm: () => void; pending: boolean }) {
  const [confirming, setConfirming] = React.useState(false);

  React.useEffect(() => {
    if (!confirming) return;
    const t = setTimeout(() => setConfirming(false), 4000);
    return () => clearTimeout(t);
  }, [confirming]);

  return (
    <Button
      variant={confirming ? "destructive" : "outline"}
      size="sm"
      disabled={pending}
      onClick={() => (confirming ? onConfirm() : setConfirming(true))}
    >
      {pending ? <Spinner className="h-3.5 w-3.5" /> : confirming ? "Confirm delete" : "Delete"}
    </Button>
  );
}
