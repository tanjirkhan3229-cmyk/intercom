"use client";

/**
 * TanStack Query data layer for the inbox. Lists are keyset-paginated infinite queries (no OFFSET,
 * RFC-002 §6). The realtime layer invalidates these keys so the cache re-reads from Postgres;
 * sends are optimistic and reconciled by part id (RFC P0.5 acceptance).
 */
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type InfiniteData,
} from "@tanstack/react-query";
import type { Page } from "@relay/shared";
import { useApi, useAuth } from "./auth";
import { qk } from "./keys";
import { paramsForView, type ListParams } from "./views";
import type { Conversation, Part } from "./types";

const nextCursor = (last: Page<unknown>) => last.next_cursor ?? undefined;

// --- Reference data -----------------------------------------------------------

export function useTeams() {
  const api = useApi();
  const { status } = useAuth();
  return useQuery({
    queryKey: qk.teams,
    queryFn: () => api.teams(),
    enabled: status === "authenticated",
  });
}

export function useSavedReplies() {
  const api = useApi();
  const { status } = useAuth();
  return useQuery({
    queryKey: qk.savedReplies,
    queryFn: () => api.listSavedReplies(),
    enabled: status === "authenticated",
    staleTime: 5 * 60_000,
  });
}

// --- Conversation list --------------------------------------------------------

export function useConversations(viewId: string) {
  const api = useApi();
  const { session, status } = useAuth();
  const params: ListParams = paramsForView(viewId, session?.admin.id);
  return useInfiniteQuery({
    queryKey: qk.conversations(params),
    queryFn: ({ pageParam }) =>
      api.listConversations({ ...params, cursor: pageParam as string | undefined }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: nextCursor,
    enabled: status === "authenticated",
  });
}

export function useConversation(id: string | null) {
  const api = useApi();
  return useQuery({
    queryKey: id ? qk.conversation(id) : ["conversation", "none"],
    queryFn: () => api.getConversation(id!),
    enabled: !!id,
  });
}

// --- Thread parts -------------------------------------------------------------

export function useParts(id: string | null) {
  const api = useApi();
  return useInfiniteQuery({
    queryKey: id ? qk.parts(id) : ["parts", "none"],
    queryFn: ({ pageParam }) => api.listParts(id!, pageParam as string | undefined),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: nextCursor,
    enabled: !!id,
  });
}

/** Flatten a newest-first paginated parts cache into chronological (oldest→newest) order. */
export function flattenParts(data: InfiniteData<Page<Part>> | undefined): Part[] {
  if (!data) return [];
  const newestFirst = data.pages.flatMap((p) => p.items);
  return [...newestFirst].reverse();
}

// --- Tags / contact -----------------------------------------------------------

export function useTags(id: string | null) {
  const api = useApi();
  return useQuery({
    queryKey: id ? qk.tags(id) : ["tags", "none"],
    queryFn: () => api.listTags(id!),
    enabled: !!id,
  });
}

export function useContact(id: string | null) {
  const api = useApi();
  return useQuery({
    queryKey: id ? qk.contact(id) : ["contact", "none"],
    queryFn: () => api.getContact(id!),
    enabled: !!id,
  });
}

export function useContactConversations(id: string | null) {
  const api = useApi();
  return useQuery({
    queryKey: id ? qk.contactConversations(id) : ["contact-conversations", "none"],
    queryFn: () => api.listContactConversations(id!).then((p) => p.items),
    enabled: !!id,
  });
}

export function useContactEvents(id: string | null) {
  const api = useApi();
  return useQuery({
    queryKey: id ? qk.contactEvents(id) : ["contact-events", "none"],
    queryFn: () => api.listContactEvents(id!),
    enabled: !!id,
  });
}

// --- Mutations ----------------------------------------------------------------

function tempId(): string {
  return `temp_${globalThis.crypto?.randomUUID?.() ?? Date.now()}`;
}

/** Prepend an optimistic part to the newest page of the thread cache. */
function optimisticPart(
  conversationId: string,
  authorId: string | undefined,
  body: string,
  partType: "comment" | "note",
): Part {
  return {
    id: tempId(),
    conversation_id: conversationId,
    author_kind: "admin",
    author_id: authorId ?? null,
    part_type: partType,
    body,
    attachments: [],
    meta: { optimistic: true },
    created_at: new Date().toISOString(),
  };
}

export interface SendInput {
  body: string;
  attachments?: unknown[];
}

function useSendPart(conversationId: string, kind: "comment" | "note") {
  const api = useApi();
  const qc = useQueryClient();
  const { session } = useAuth();
  const key = qk.parts(conversationId);

  return useMutation({
    mutationFn: ({ body, attachments }: SendInput) =>
      kind === "comment"
        ? api.reply(conversationId, body, attachments ?? [])
        : api.note(conversationId, body),
    onMutate: async ({ body }: SendInput) => {
      await qc.cancelQueries({ queryKey: key });
      const prev = qc.getQueryData<InfiniteData<Page<Part>>>(key);
      const optimistic = optimisticPart(conversationId, session?.admin.id, body, kind);
      qc.setQueryData<InfiniteData<Page<Part>>>(key, (old) => {
        if (!old || old.pages.length === 0) {
          return {
            pages: [{ items: [optimistic], next_cursor: null }],
            pageParams: [undefined],
          } as InfiniteData<Page<Part>>;
        }
        const pages = old.pages.slice();
        pages[0] = { ...pages[0]!, items: [optimistic, ...pages[0]!.items] };
        return { ...old, pages };
      });
      return { prev, tempPartId: optimistic.id };
    },
    onError: (_err, _body, ctx) => {
      if (ctx?.prev) qc.setQueryData(key, ctx.prev);
    },
    onSuccess: (real, _body, ctx) => {
      // Reconcile by part id: swap the optimistic placeholder for the server's real part.
      qc.setQueryData<InfiniteData<Page<Part>>>(key, (old) => {
        if (!old) return old;
        const pages = old.pages.map((p) => ({
          ...p,
          items: p.items.map((it) => (it.id === ctx?.tempPartId ? real : it)),
        }));
        return { ...old, pages };
      });
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: key });
      void qc.invalidateQueries({ queryKey: qk.conversation(conversationId) });
      void qc.invalidateQueries({ queryKey: qk.conversationsRoot });
    },
  });
}

export function useReply(conversationId: string) {
  return useSendPart(conversationId, "comment");
}
export function useNote(conversationId: string) {
  return useSendPart(conversationId, "note");
}

function useConversationMutation<TArgs>(
  conversationId: string,
  fn: (api: ReturnType<typeof useApi>, args: TArgs) => Promise<Conversation>,
) {
  const api = useApi();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (args: TArgs) => fn(api, args),
    onSuccess: (conv) => {
      qc.setQueryData(qk.conversation(conversationId), conv);
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: qk.conversation(conversationId) });
      void qc.invalidateQueries({ queryKey: qk.conversationsRoot });
    },
  });
}

export function useSetState(conversationId: string) {
  return useConversationMutation<{ state: string; snoozedUntil?: string }>(
    conversationId,
    (api, { state, snoozedUntil }) => api.setState(conversationId, state, snoozedUntil),
  );
}

export function useAssign(conversationId: string) {
  return useConversationMutation<{ assigneeId?: string | null; teamId?: string | null }>(
    conversationId,
    (api, { assigneeId, teamId }) => api.assign(conversationId, assigneeId, teamId),
  );
}

export function useTagMutations(conversationId: string) {
  const api = useApi();
  const qc = useQueryClient();
  const invalidate = () => void qc.invalidateQueries({ queryKey: qk.tags(conversationId) });
  const add = useMutation({
    mutationFn: (name: string) => api.addTag(conversationId, name),
    onSettled: invalidate,
  });
  const remove = useMutation({
    mutationFn: (name: string) => api.removeTag(conversationId, name),
    onSettled: invalidate,
  });
  return { add, remove };
}
