"use client";

import * as React from "react";
import { useCollections, useCollectionMutations } from "@/lib/hc-hooks";
import { Button } from "@/components/ui/button";
import { Input, Badge, Spinner } from "@/components/ui/primitives";
import { LoadingState, ErrorState } from "@/components/inbox/states";
import type { Collection } from "@/lib/types";

/**
 * Collections manager (RFC P0.8): list with article counts, create, inline rename, delete.
 * Mutations invalidate both the collections list and the articles root (counts move with articles).
 */
export function CollectionsPanel() {
  const query = useCollections();
  const { create } = useCollectionMutations();
  const [name, setName] = React.useState("");

  const onCreate = () => {
    const trimmed = name.trim();
    if (!trimmed) return;
    create.mutate({ name: trimmed });
    setName("");
  };

  return (
    <div className="flex flex-col gap-3" data-testid="collections-panel">
      <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        Collections
      </p>

      <form
        className="flex items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          onCreate();
        }}
      >
        <Input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="New collection name"
          className="h-8"
        />
        <Button type="submit" size="sm" disabled={!name.trim() || create.isPending}>
          {create.isPending ? <Spinner className="h-3.5 w-3.5" /> : "Add"}
        </Button>
      </form>

      {query.isLoading ? (
        <LoadingState label="Loading collections…" className="h-24" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} className="h-24" />
      ) : query.data && query.data.length > 0 ? (
        <ul className="flex flex-col gap-1">
          {query.data.map((c) => (
            <CollectionRow key={c.id} collection={c} />
          ))}
        </ul>
      ) : (
        <p className="text-xs text-muted-foreground">No collections yet.</p>
      )}
    </div>
  );
}

function CollectionRow({ collection }: { collection: Collection }) {
  const { update, remove } = useCollectionMutations();
  const [editing, setEditing] = React.useState(false);
  const [name, setName] = React.useState(collection.name);
  const [confirming, setConfirming] = React.useState(false);

  React.useEffect(() => {
    if (!confirming) return;
    const t = setTimeout(() => setConfirming(false), 4000);
    return () => clearTimeout(t);
  }, [confirming]);

  const commit = () => {
    const trimmed = name.trim();
    setEditing(false);
    if (trimmed && trimmed !== collection.name) {
      update.mutate({ id: collection.id, input: { name: trimmed } });
    } else {
      setName(collection.name);
    }
  };

  return (
    <li className="group flex items-center gap-2 rounded-md px-2 py-1.5 hover:bg-accent/50">
      {editing ? (
        <Input
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") commit();
            if (e.key === "Escape") {
              setName(collection.name);
              setEditing(false);
            }
          }}
          className="h-7"
        />
      ) : (
        <button
          type="button"
          onClick={() => setEditing(true)}
          className="min-w-0 flex-1 truncate text-left text-sm"
          title="Click to rename"
        >
          {collection.name}
        </button>
      )}

      <Badge variant="muted" className="shrink-0">
        {collection.article_count}
      </Badge>

      <Button
        variant="ghost"
        size="icon"
        className="h-7 w-7 shrink-0 text-destructive"
        aria-label={`Delete ${collection.name}`}
        disabled={remove.isPending}
        onClick={() => (confirming ? remove.mutate(collection.id) : setConfirming(true))}
        title={confirming ? "Click again to confirm" : "Delete"}
      >
        {remove.isPending ? <Spinner className="h-3.5 w-3.5" /> : confirming ? "?" : "✕"}
      </Button>
    </li>
  );
}
