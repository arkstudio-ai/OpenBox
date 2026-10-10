// One table read, three audited writes. Reads are not audited server-side, so
// they may refetch freely; writes carry a caller-owned request key and the
// revision they saw, and the table is refetched after every one.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useAuthStore } from "@/shared/api/auth-store"
import { http } from "@/shared/api/http"
import type {
  PricingHistory,
  PricingPreviewResult,
  PricingTable,
  PricingWriteBody,
  PricingWriteResult,
  PriceFragment,
} from "./types"

const BASE = "/api/admin/pricing"

function useUserId(): string {
  return useAuthStore((s) => s.user?.id ?? "anonymous")
}

export const pricingKeys = {
  all: (userId: string) => ["admin-pricing", userId] as const,
  table: (userId: string) => ["admin-pricing", userId, "table"] as const,
  history: (userId: string, key: string) => ["admin-pricing", userId, "history", key] as const,
}

export function usePricingTable() {
  const userId = useUserId()
  return useQuery({
    queryKey: pricingKeys.table(userId),
    queryFn: () => http.get<PricingTable>(BASE),
    staleTime: 15_000,
  })
}

export function usePricingHistory(key: string | null) {
  const userId = useUserId()
  return useQuery({
    queryKey: pricingKeys.history(userId, key ?? ""),
    queryFn: () => http.get<PricingHistory>(`${BASE}/${encodeURIComponent(key ?? "")}/history`),
    enabled: Boolean(key),
  })
}

function useRefetchAfterWrite() {
  const userId = useUserId()
  const cache = useQueryClient()
  return async () => {
    if (useAuthStore.getState().user?.id !== userId) return
    await cache.invalidateQueries({ queryKey: pricingKeys.all(userId) })
    // The composer's tier prices come from the same catalogue.
    await cache.invalidateQueries({ queryKey: ["agent-config"] })
  }
}

export function useWritePricing() {
  const refetch = useRefetchAfterWrite()
  return useMutation({
    retry: false,
    mutationFn: ({ key, body }: { key: string; body: PricingWriteBody }) =>
      http.put<PricingWriteResult>(`${BASE}/${encodeURIComponent(key)}`, body),
    onSuccess: refetch,
  })
}

export function useRevertPricing() {
  const refetch = useRefetchAfterWrite()
  return useMutation({
    retry: false,
    mutationFn: ({
      key,
      body,
    }: {
      key: string
      body: { request_key: string; reason: string; expected_revision: number }
    }) => http.post<PricingWriteResult>(`${BASE}/${encodeURIComponent(key)}/revert`, body),
    onSuccess: refetch,
  })
}

export function usePricingPreview() {
  return useMutation({
    retry: false,
    mutationFn: (body: { key: string; usage: Record<string, number>; sale?: PriceFragment | null }) =>
      http.post<PricingPreviewResult>(`${BASE}/preview`, body),
  })
}

export function exportUrl(): string {
  return `${BASE}/export`
}
