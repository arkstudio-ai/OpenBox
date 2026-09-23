// Store data hooks. Components never fetch directly (ENGINEERING_SPEC §7).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import type { StarterCard, StarterCards, Store, StoreCreateInput, StoreList, StoreUpdateInput } from "../types"
import { storeKeys } from "./keys"

export function useUserId(): string {
  return useAuthStore((s) => s.user?.id ?? "anon")
}

export function useWorkspaceId(): string {
  return useWorkspaceStore((s) => s.currentId ?? "none")
}

export function useStoreQuery() {
  const userId = useUserId()
  const workspaceId = useWorkspaceId()
  return useQuery({
    queryKey: storeKeys.list(userId, workspaceId),
    queryFn: ({ signal }) => http.get<StoreList>("/api/stores", { signal }),
    staleTime: 60_000,
  })
}

/** The workspace's store, or null once the list has loaded empty; undefined
 *  while it is still unknown so a first-run prompt never fires on a guess. */
export function useCurrentStore(): Store | null | undefined {
  const query = useStoreQuery()
  const items = query.data?.items
  if (!Array.isArray(items)) return undefined
  return items[0] ?? null
}

function useRefreshStore() {
  const qc = useQueryClient()
  const userId = useUserId()
  return () => {
    void qc.invalidateQueries({ queryKey: storeKeys.all(userId) })
  }
}

export function useCreateStore() {
  const qc = useQueryClient()
  const userId = useUserId()
  const workspaceId = useWorkspaceId()
  const refresh = useRefreshStore()
  return useMutation({
    mutationFn: (input: StoreCreateInput) => http.post<Store>("/api/stores", input),
    onSuccess: (store) => {
      // Show the new store at once; the invalidation confirms it.
      qc.setQueryData<StoreList>(storeKeys.list(userId, workspaceId), (prev) =>
        prev ? { ...prev, items: [store] } : prev,
      )
      refresh()
    },
  })
}

export function useUpdateStore() {
  const refresh = useRefreshStore()
  return useMutation({
    mutationFn: ({ id, ...patch }: StoreUpdateInput & { id: string }) =>
      http.patch<Store>(`/api/stores/${encodeURIComponent(id)}`, patch),
    onSuccess: refresh,
  })
}

export function useStarterCards(storeId: string | null | undefined, locale: string) {
  const userId = useUserId()
  const workspaceId = useWorkspaceId()
  return useQuery({
    queryKey: storeKeys.starterCards(userId, workspaceId, storeId ?? "none", locale),
    queryFn: ({ signal }) =>
      http.get<StarterCards>(
        `/api/stores/${encodeURIComponent(storeId ?? "")}/starter-cards?locale=${encodeURIComponent(locale)}`,
        { signal },
      ),
    enabled: Boolean(storeId),
    // The backend caches a day's cards; a persona confirmation invalidates
    // through `store.updated`.
    staleTime: 10 * 60_000,
  })
}

/** Cards for the empty chat page in the viewer's language, or null while the
 *  store or its cards are unknown — the page then keeps its locale copy. */
export function useStarterSuggestions(): StarterCard[] | null {
  const { i18n } = useTranslation()
  const store = useCurrentStore()
  const cards = useStarterCards(store?.id, i18n.language)
  const items = cards.data?.items
  if (!store || !Array.isArray(items) || items.length === 0) return null
  return items
}

export function useRegeneratePersona() {
  const refresh = useRefreshStore()
  return useMutation({
    mutationFn: (storeId: string) =>
      http.post<{ ok: boolean; sessionId: string }>(
        `/api/stores/${encodeURIComponent(storeId)}/persona/regenerate`,
        undefined,
      ),
    onSuccess: refresh,
  })
}
