"use client";

import { Paperclip } from "lucide-react";
import * as React from "react";
import { useApi } from "@/lib/auth";
import { flattenParts, useContact, useConversation, useParts } from "@/lib/hooks";
import { useThreadRealtime } from "@/lib/realtime-hooks";
import { contactLabel, initials, timeAgo } from "@/lib/format";
import { Avatar, Badge } from "@/components/ui/primitives";
import { Button as ActionButton } from "@/components/ui/button";
import { AssignMenu, SnoozeMenu, StateButton, TagEditor } from "./conversation-actions";
import { Composer, type ComposerHandle } from "./composer";
import { EmptyState, ErrorState, LoadingState } from "./states";
import { cn } from "@/lib/utils";
import type { Attachment, Part } from "@/lib/types";

export function Thread({
  conversationId,
  composerRef,
}: {
  conversationId: string;
  composerRef: React.RefObject<ComposerHandle | null>;
}) {
  const conversation = useConversation(conversationId);
  const partsQuery = useParts(conversationId);
  const contact = useContact(conversation.data?.contact_id ?? null);
  useThreadRealtime(conversationId);

  const parts = React.useMemo(() => flattenParts(partsQuery.data), [partsQuery.data]);
  const bottomRef = React.useRef<HTMLDivElement>(null);
  const newestId = parts[parts.length - 1]?.id;

  // Keep the newest message in view as the thread grows / on conversation switch.
  React.useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [conversationId, newestId]);

  if (conversation.isLoading) return <LoadingState label="Loading conversation…" />;
  if (conversation.isError)
    return <ErrorState error={conversation.error} onRetry={() => void conversation.refetch()} />;
  if (!conversation.data) return null;

  const conv = conversation.data;
  const title = contact.data ? contactLabel(contact.data) : conv.contact_id;

  return (
    <div className="flex h-full flex-col">
      <header className="border-b border-border px-4 py-2.5">
        <div className="flex items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2">
            <Avatar label={initials(title)} />
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold">{title}</p>
              <p className="text-[11px] capitalize text-muted-foreground">
                {conv.state}
                {conv.channel !== "chat" ? ` · ${conv.channel}` : ""}
              </p>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            <AssignMenu conversation={conv} />
            <SnoozeMenu conversation={conv} />
            <StateButton conversation={conv} />
          </div>
        </div>
        <div className="mt-2">
          <TagEditor conversationId={conversationId} />
        </div>
      </header>

      <div className="flex-1 overflow-y-auto px-4 py-4" data-testid="thread-timeline">
        {partsQuery.hasNextPage && (
          <div className="mb-3 flex justify-center">
            <ActionButton
              variant="outline"
              size="sm"
              onClick={() => void partsQuery.fetchNextPage()}
              disabled={partsQuery.isFetchingNextPage}
            >
              {partsQuery.isFetchingNextPage ? "Loading…" : "Load earlier messages"}
            </ActionButton>
          </div>
        )}
        {partsQuery.isLoading ? (
          <LoadingState label="Loading messages…" />
        ) : parts.length === 0 ? (
          <EmptyState title="No messages yet" hint="Send the first reply below." />
        ) : (
          <ul className="space-y-3">
            {parts.map((p) => (
              <PartItem key={p.id} part={p} contactName={title} />
            ))}
          </ul>
        )}
        <div ref={bottomRef} />
      </div>

      <Composer conversationId={conversationId} contactId={conv.contact_id} ref={composerRef} />
    </div>
  );
}

function PartItem({ part, contactName }: { part: Part; contactName: string }) {
  // System-ish parts render as a centered inline line, not a bubble.
  if (part.part_type === "assignment" || part.part_type === "state_change") {
    return (
      <li className="flex justify-center">
        <span className="rounded-full bg-muted px-3 py-0.5 text-[11px] text-muted-foreground">
          {describeSystemPart(part)} · {timeAgo(part.created_at)}
        </span>
      </li>
    );
  }
  if (part.part_type === "rating") {
    return (
      <li className="flex justify-center">
        <Badge variant="muted">Rated {String(part.meta.rating ?? "")}/5 · {timeAgo(part.created_at)}</Badge>
      </li>
    );
  }

  const isNote = part.part_type === "note";
  const fromContact = part.author_kind === "contact";
  const optimistic = part.meta.optimistic === true;

  return (
    <li className={cn("flex", fromContact ? "justify-start" : "justify-end")}>
      <div
        className={cn(
          "max-w-[76%] rounded-2xl px-3.5 py-2 text-sm shadow-sm",
          isNote
            ? "bg-amber-100 text-amber-950 dark:bg-amber-950/40 dark:text-amber-100"
            : fromContact
              ? "bg-muted text-foreground"
              : "bg-primary text-primary-foreground",
          optimistic && "opacity-60",
        )}
      >
        <div className="mb-0.5 flex items-center gap-2 text-[11px] opacity-70">
          <span>{fromContact ? contactName : isNote ? "Note" : "You"}</span>
          <span>{timeAgo(part.created_at)}</span>
        </div>
        {part.body && <p className="whitespace-pre-wrap break-words">{part.body}</p>}
        {part.attachments.length > 0 && (
          <div className="mt-1.5 space-y-1">
            {part.attachments.map((a, i) => (
              <AttachmentLink key={i} attachment={a} />
            ))}
          </div>
        )}
      </div>
    </li>
  );
}

function AttachmentLink({ attachment }: { attachment: Attachment }) {
  const api = useApi();
  const open = async () => {
    const key = typeof attachment.key === "string" ? attachment.key : undefined;
    const direct = typeof attachment.url === "string" ? attachment.url : undefined;
    const href = key ? (await api.attachmentDownloadUrl(key)).url : direct;
    if (href) window.open(href, "_blank", "noopener");
  };
  return (
    <button
      onClick={() => void open()}
      className="flex items-center gap-1 text-xs underline underline-offset-2 opacity-90 hover:opacity-100"
    >
      <Paperclip className="h-3 w-3" />
      {attachment.name ?? "attachment"}
    </button>
  );
}

function describeSystemPart(part: Part): string {
  if (part.part_type === "assignment") return "Assignment changed";
  if (part.part_type === "state_change") {
    const to = part.meta.to;
    return typeof to === "string" ? `Marked ${to}` : "State changed";
  }
  return "Update";
}
