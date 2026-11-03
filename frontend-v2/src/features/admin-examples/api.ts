// One list read with server-side filters, the taxonomy, and audited writes;
// every write refetches the list. Previews trade an asset id for a short link
// through the admin route, because the console is not the asset's owner.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useAuthStore } from "@/shared/api/auth-store"
import { http } from "@/shared/api/http"
import type { AssetLink, Example, ExampleDraft, ExampleList, ExampleWrite, Taxonomy } from "./types"

export const BASE = "/api/admin/examples"
export const PAGE_SIZE = 50

export interface ListFilters {
  entry: string
  industry: string
  shopType: string
  status: string
  q: string
  offset: string
}

function useUserId(): string {
  return useAuthStore((s) => s.user?.id ?? "anonymous")
}

export const exampleKeys = {
  all: (userId: string) => ["admin-examples", userId] as const,
  list: (userId: string, query: string) => ["admin-examples", userId, "list", query] as const,
  taxonomy: (userId: string) => ["admin-examples", userId, "taxonomy"] as const,
  asset: (userId: string, assetId: string) => ["admin-examples", userId, "asset", assetId] as const,
}

export function listQuery(filters: ListFilters): string {
  const search = new URLSearchParams()
  if (filters.entry) search.set("entry_id", filters.entry)
  if (filters.industry) search.set("industry", filters.industry)
  if (filters.shopType) search.set("shop_type", filters.shopType)
  if (filters.status) search.set("status", filters.status)
  if (filters.q.trim()) search.set("q", filters.q.trim())
  search.set("limit", String(PAGE_SIZE))
  if (filters.offset && filters.offset !== "0") search.set("offset", filters.offset)
  return `?${search.toString()}`
}

export function useExamples(filters: ListFilters) {
  const userId = useUserId()
  const query = listQuery(filters)
  return useQuery({
    queryKey: exampleKeys.list(userId, query),
    queryFn: ({ signal }) => http.get<ExampleList>(`${BASE}${query}`, { signal }),
    staleTime: 15_000,
  })
}

export function useTaxonomy() {
  const userId = useUserId()
  return useQuery({
    queryKey: exampleKeys.taxonomy(userId),
    queryFn: ({ signal }) => http.get<Taxonomy>(`${BASE}/taxonomy`, { signal }),
    staleTime: 60 * 60_000,
  })
}

export function useAssetLink(assetId: string | null | undefined) {
  const userId = useUserId()
  return useQuery({
    queryKey: exampleKeys.asset(userId, assetId ?? "none"),
    queryFn: () => http.get<AssetLink>(`${BASE}/assets/${encodeURIComponent(assetId ?? "")}/url`),
    enabled: !!assetId,
    staleTime: 40 * 60_000,
    retry: 1,
  })
}

export const prefillExample = (sessionId: string, assetId?: string | null) =>
  http.post<ExampleDraft>(`${BASE}/prefill`, { session_id: sessionId, asset_id: assetId || null })

function useRefetchAfterWrite() {
  const userId = useUserId()
  const cache = useQueryClient()
  return async () => {
    if (useAuthStore.getState().user?.id !== userId) return
    await cache.invalidateQueries({ queryKey: exampleKeys.all(userId) })
  }
}

export function useSaveExample() {
  const refetch = useRefetchAfterWrite()
  return useMutation({
    retry: false,
    mutationFn: ({ id, body }: { id: string | null; body: ExampleWrite }) =>
      id ? http.put<Example>(`${BASE}/${encodeURIComponent(id)}`, body) : http.post<Example>(BASE, body),
    onSuccess: refetch,
  })
}

export function useSetExampleStatus() {
  const refetch = useRefetchAfterWrite()
  return useMutation({
    retry: false,
    mutationFn: ({ id, status }: { id: string; status: "active" | "disabled" }) =>
      http.post<Example>(`${BASE}/${encodeURIComponent(id)}/status`, { status }),
    onSuccess: refetch,
  })
}
