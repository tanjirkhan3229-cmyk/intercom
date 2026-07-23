"use client";

/**
 * TanStack Query data layer for the Help Center editor (P0.8), matching the inbox's hook style
 * (`lib/hooks.ts`): keyset-paginated lists, mutations that invalidate the affected keys. The
 * editor autosaves via `useUpdateArticle`; publish/unpublish flip status server-side (which also
 * emits the ISR revalidation event) and we refetch the article + list.
 */
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Page } from "@relay/shared";
import { useApi, useAuth } from "./auth";
import { qk } from "./keys";
import type {
  ArticleInput,
  ArticleSummary,
  CollectionInput,
  HelpCenterInput,
  Source,
  SourceInput,
} from "./types";

const nextCursor = (last: Page<unknown>) => last.next_cursor ?? undefined;

// --- Collections --------------------------------------------------------------

export function useCollections() {
  const api = useApi();
  const { status } = useAuth();
  return useQuery({
    queryKey: qk.collections,
    queryFn: () => api.listCollections(),
    enabled: status === "authenticated",
  });
}

export function useCollectionMutations() {
  const api = useApi();
  const qc = useQueryClient();
  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: qk.collections });
    void qc.invalidateQueries({ queryKey: qk.articlesRoot });
  };
  const create = useMutation({
    mutationFn: (input: CollectionInput) => api.createCollection(input),
    onSettled: invalidate,
  });
  const update = useMutation({
    mutationFn: ({ id, input }: { id: string; input: CollectionInput }) =>
      api.updateCollection(id, input),
    onSettled: invalidate,
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteCollection(id),
    onSettled: invalidate,
  });
  return { create, update, remove };
}

// --- Articles -----------------------------------------------------------------

export function useArticles(params: { status?: string; collectionId?: string } = {}) {
  const api = useApi();
  const { status } = useAuth();
  return useInfiniteQuery({
    queryKey: qk.articles(params),
    queryFn: ({ pageParam }) =>
      api.listArticles({ ...params, cursor: pageParam as string | undefined }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: nextCursor,
    enabled: status === "authenticated",
  });
}

export function flattenArticles(
  pages: { items: ArticleSummary[] }[] | undefined,
): ArticleSummary[] {
  return pages ? pages.flatMap((p) => p.items) : [];
}

export function useArticle(id: string | null) {
  const api = useApi();
  return useQuery({
    queryKey: id ? qk.article(id) : ["article", "none"],
    queryFn: () => api.getArticle(id!),
    enabled: !!id,
  });
}

export function useCreateArticle() {
  const api = useApi();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: ArticleInput) => api.createArticle(input),
    onSuccess: () => void qc.invalidateQueries({ queryKey: qk.articlesRoot }),
  });
}

export function useUpdateArticle(id: string) {
  const api = useApi();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: ArticleInput) => api.updateArticle(id, input),
    onSuccess: (article) => {
      qc.setQueryData(qk.article(id), article);
      void qc.invalidateQueries({ queryKey: qk.articlesRoot });
    },
  });
}

export function useArticleStatusMutations(id: string) {
  const api = useApi();
  const qc = useQueryClient();
  const onSuccess = () => {
    void qc.invalidateQueries({ queryKey: qk.article(id) });
    void qc.invalidateQueries({ queryKey: qk.articlesRoot });
  };
  const publish = useMutation({ mutationFn: () => api.publishArticle(id), onSuccess });
  const unpublish = useMutation({ mutationFn: () => api.unpublishArticle(id), onSuccess });
  const remove = useMutation({
    mutationFn: () => api.deleteArticle(id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: qk.articlesRoot }),
  });
  return { publish, unpublish, remove };
}

// --- Help center config -------------------------------------------------------

export function useHelpCenter() {
  const api = useApi();
  const { status } = useAuth();
  return useQuery({
    queryKey: qk.helpCenter,
    queryFn: () => api.getHelpCenter(),
    enabled: status === "authenticated",
  });
}

export function useUpdateHelpCenter() {
  const api = useApi();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: HelpCenterInput) => api.updateHelpCenter(input),
    onSuccess: (cfg) => qc.setQueryData(qk.helpCenter, cfg),
  });
}

// --- Knowledge Hub sources (P1.1) ---------------------------------------------

/** Sources list; auto-polls while any source is still ingesting so the status badge stays live. */
export function useSources() {
  const api = useApi();
  const { status } = useAuth();
  return useQuery({
    queryKey: qk.sources,
    queryFn: () => api.listSources(),
    enabled: status === "authenticated",
    refetchInterval: (query) => {
      const data = query.state.data as Source[] | undefined;
      const busy = data?.some((s) => s.status === "syncing" || s.status === "pending");
      return busy ? 3000 : false;
    },
  });
}

export function useSourceMutations() {
  const api = useApi();
  const qc = useQueryClient();
  const invalidate = () => void qc.invalidateQueries({ queryKey: qk.sources });
  const create = useMutation({
    mutationFn: (input: SourceInput) => api.createSource(input),
    onSettled: invalidate,
  });
  const sync = useMutation({ mutationFn: (id: string) => api.syncSource(id), onSettled: invalidate });
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteSource(id),
    onSettled: invalidate,
  });
  return { create, sync, remove };
}
