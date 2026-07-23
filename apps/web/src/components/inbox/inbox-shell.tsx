"use client";

import { MessageSquare } from "lucide-react";
import * as React from "react";
import { useAuth } from "@/lib/auth";
import { useAssign, useConversation, useSetState } from "@/lib/hooks";
import { useInboxRealtime, usePresenceHeartbeat } from "@/lib/realtime-hooks";
import { useShortcuts } from "@/lib/shortcuts";
import { ViewsSidebar } from "./views-sidebar";
import { ConversationList } from "./conversation-list";
import { ContactPanel } from "./contact-panel";
import { Thread } from "./thread";
import { snoozePresets } from "./conversation-actions";
import { EmptyState } from "./states";
import type { ComposerHandle } from "./composer";

/**
 * The agent inbox: three panes (views · list · thread) + a contact side panel. View and selected
 * conversation are lifted to the page (URL-synced, so refresh restores the exact view — RFC P0.5
 * acceptance). The shell owns j/k list navigation and the realtime inbox subscription; the thread
 * pane owns the a/s/e/r/n conversation shortcuts.
 */
export function InboxShell({
  view,
  onView,
  selectedId,
  onSelect,
}: {
  view: string;
  onView: (viewId: string) => void;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
}) {
  const orderRef = React.useRef<string[]>([]);
  const [openedIds, setOpenedIds] = React.useState<Set<string>>(() => new Set());
  const composerRef = React.useRef<ComposerHandle | null>(null);

  useInboxRealtime(view);
  usePresenceHeartbeat();

  const select = React.useCallback(
    (id: string | null) => {
      if (id) setOpenedIds((s) => (s.has(id) ? s : new Set(s).add(id)));
      onSelect(id);
    },
    [onSelect],
  );

  const registerOrder = React.useCallback((ids: string[]) => {
    orderRef.current = ids;
  }, []);

  // j/k move through the list in its on-screen order.
  const navigate = React.useCallback(
    (delta: number) => {
      const order = orderRef.current;
      if (order.length === 0) return;
      const idx = selectedId ? order.indexOf(selectedId) : -1;
      const next = idx < 0 ? 0 : Math.min(Math.max(idx + delta, 0), order.length - 1);
      select(order[next]!);
    },
    [selectedId, select],
  );

  useShortcuts({ j: () => navigate(1), k: () => navigate(-1) });

  return (
    <div className="grid h-screen grid-cols-[220px_360px_1fr_300px] divide-x divide-border">
      <ViewsSidebar activeView={view} onSelect={(v) => { onView(v); select(null); }} />

      <div className="flex min-h-0 flex-col">
        <div className="border-b border-border px-4 py-3 text-sm font-semibold capitalize">
          {view.startsWith("team:") ? "Team inbox" : view.replace("-", " ")}
        </div>
        <div className="min-h-0 flex-1">
          <ConversationList
            viewId={view}
            selectedId={selectedId}
            onSelect={select}
            openedIds={openedIds}
            registerOrder={registerOrder}
          />
        </div>
      </div>

      {selectedId ? (
        <ThreadPane conversationId={selectedId} composerRef={composerRef} />
      ) : (
        <EmptyState
          title="No conversation selected"
          hint="Pick a conversation from the list, or use j / k to move through it."
          icon={<MessageSquare className="h-8 w-8" />}
        />
      )}

      {selectedId ? (
        <SelectedContactPanel conversationId={selectedId} onSelectConversation={select} />
      ) : (
        <div className="bg-muted/20" />
      )}
    </div>
  );
}

/** Thread + the a/s/e/r/n shortcuts (all need a concrete conversation id, so they live here). */
function ThreadPane({
  conversationId,
  composerRef,
}: {
  conversationId: string;
  composerRef: React.RefObject<ComposerHandle | null>;
}) {
  const { session } = useAuth();
  const conversation = useConversation(conversationId);
  const assign = useAssign(conversationId);
  const setState = useSetState(conversationId);

  useShortcuts({
    a: () => assign.mutate({ assigneeId: session?.admin.id }),
    s: () => setState.mutate({ state: "snoozed", snoozedUntil: snoozePresets()[2]!.until }),
    e: () =>
      setState.mutate({ state: conversation.data?.state === "closed" ? "open" : "closed" }),
    r: () => composerRef.current?.focusReply(),
    n: () => composerRef.current?.focusNote(),
  });

  return <Thread conversationId={conversationId} composerRef={composerRef} />;
}

function SelectedContactPanel({
  conversationId,
  onSelectConversation,
}: {
  conversationId: string;
  onSelectConversation: (id: string) => void;
}) {
  const conversation = useConversation(conversationId);
  return (
    <ContactPanel
      contactId={conversation.data?.contact_id ?? null}
      onSelectConversation={onSelectConversation}
    />
  );
}
