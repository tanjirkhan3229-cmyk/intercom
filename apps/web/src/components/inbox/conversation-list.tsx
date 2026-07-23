"use client";

import { useVirtualizer } from "@tanstack/react-virtual";
import * as React from "react";
import { useContact, useConversations } from "@/lib/hooks";
import { contactLabel, initials, timeAgo } from "@/lib/format";
import { Avatar, Badge } from "@/components/ui/primitives";
import { EmptyState, ErrorState, LoadingState } from "./states";
import { cn } from "@/lib/utils";
import type { Conversation } from "@/lib/types";

const ROW_HEIGHT = 68;

/**
 * Middle pane: the conversation list for the active view. Server-ordered by `waiting_since`
 * (R1 index), keyset infinite-scrolled, and virtualized so 1k rows scroll without jank
 * (RFC P0.5 acceptance). Rows update in place as the realtime layer invalidates the query.
 */
export function ConversationList({
  viewId,
  selectedId,
  onSelect,
  openedIds,
  registerOrder,
}: {
  viewId: string;
  selectedId: string | null;
  onSelect: (id: string) => void;
  openedIds: Set<string>;
  registerOrder?: (ids: string[]) => void;
}) {
  const query = useConversations(viewId);
  const parentRef = React.useRef<HTMLDivElement>(null);

  const items = React.useMemo<Conversation[]>(
    () => query.data?.pages.flatMap((p) => p.items) ?? [],
    [query.data],
  );

  // Expose the current ordering so j/k navigation in the shell matches what's on screen.
  React.useEffect(() => {
    registerOrder?.(items.map((c) => c.id));
  }, [items, registerOrder]);

  const virtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 10,
  });

  // Keyset "load more": when the last row is rendered, pull the next page.
  const virtualItems = virtualizer.getVirtualItems();
  React.useEffect(() => {
    const last = virtualItems[virtualItems.length - 1];
    if (!last) return;
    if (last.index >= items.length - 1 && query.hasNextPage && !query.isFetchingNextPage) {
      void query.fetchNextPage();
    }
  }, [virtualItems, items.length, query]);

  // Keep the selected row visible when navigation changes it (e.g. j/k).
  React.useEffect(() => {
    if (!selectedId) return;
    const idx = items.findIndex((c) => c.id === selectedId);
    if (idx >= 0) virtualizer.scrollToIndex(idx, { align: "auto" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  if (query.isLoading) return <LoadingState label="Loading conversations…" />;
  if (query.isError) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  if (items.length === 0) {
    return <EmptyState title="Nothing here" hint="No conversations match this view yet." />;
  }

  return (
    <div ref={parentRef} className="h-full overflow-y-auto" data-testid="conversation-list">
      <div style={{ height: virtualizer.getTotalSize(), position: "relative", width: "100%" }}>
        {virtualItems.map((vi) => {
          const conv = items[vi.index]!;
          return (
            <div
              key={conv.id}
              style={{
                position: "absolute",
                top: 0,
                left: 0,
                width: "100%",
                height: ROW_HEIGHT,
                transform: `translateY(${vi.start}px)`,
              }}
            >
              <ConversationRow
                conv={conv}
                selected={conv.id === selectedId}
                unread={!openedIds.has(conv.id) && conv.waiting_since !== null}
                onClick={() => onSelect(conv.id)}
              />
            </div>
          );
        })}
      </div>
      {query.isFetchingNextPage && (
        <p className="py-2 text-center text-xs text-muted-foreground">Loading more…</p>
      )}
    </div>
  );
}

function ConversationRow({
  conv,
  selected,
  unread,
  onClick,
}: {
  conv: Conversation;
  selected: boolean;
  unread: boolean;
  onClick: () => void;
}) {
  const contact = useContact(conv.contact_id);
  const label = contact.data ? contactLabel(contact.data) : conv.contact_id;

  return (
    <button
      onClick={onClick}
      aria-current={selected ? "true" : undefined}
      data-testid="conversation-row"
      className={cn(
        "flex h-full w-full items-center gap-3 border-b border-border px-3 text-left transition-colors",
        selected ? "bg-accent" : "hover:bg-accent/50",
      )}
    >
      <Avatar label={initials(label)} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center justify-between gap-2">
          <span className={cn("truncate text-sm", unread ? "font-semibold" : "font-medium")}>
            {label}
          </span>
          <span className="shrink-0 text-[11px] text-muted-foreground">
            {timeAgo(conv.last_part_at)}
          </span>
        </div>
        <div className="mt-0.5 flex items-center gap-1.5">
          {unread && <span className="h-2 w-2 shrink-0 rounded-full bg-primary" aria-label="unread" />}
          {conv.channel !== "chat" && (
            <Badge variant="muted" className="px-1.5 py-0">
              {conv.channel}
            </Badge>
          )}
          {conv.waiting_since ? (
            <span className="truncate text-[11px] text-amber-600 dark:text-amber-500">
              waiting {timeAgo(conv.waiting_since)}
            </span>
          ) : (
            <span className="truncate text-[11px] text-muted-foreground">
              {conv.assignee_id ? "assigned" : "unassigned"}
            </span>
          )}
        </div>
      </div>
    </button>
  );
}
