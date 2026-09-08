// Operator billing reads. Components never fetch directly (ENGINEERING_SPEC §7).
//
// There are no mutations in this module on purpose: §3-Q5 keeps grants, refunds
// and expiry edits out of this round, and the server exposes no write route to
// call even if a component tried.
import { keepPreviousData, useQuery } from "@tanstack/react-query"
import { useAuthStore } from "@/shared/api/auth-store"
import { http } from "@/shared/api/http"
import { appendFilter } from "@/features/admin-billing/lib/filters"
import type {
  AdminOrderRow,
  AdminPage,
  OrderQuery,
  SubscriptionQuery,
  SubscriptionRow,
  WorkspaceBillingDetail,
} from "@/features/admin-billing/types"
import { adminBillingKeys } from "./keys"

function useUserId(): string {
  return useAuthStore((s) => s.user?.id ?? "anonymous")
}

// Every one of these GETs writes an `admin.view_billing` audit row (§4.7). A
// window-focus refetch would therefore forge a trail of "views" the operator
// never made, so the reads stay explicit: no focus refetch, no interval.
const READ_ONCE = {
  refetchOnWindowFocus: false,
  staleTime: 30_000,
} as const

function page(query: { offset: number; limit: number }): URLSearchParams {
  return new URLSearchParams({ offset: String(query.offset), limit: String(query.limit) })
}

export function useAdminSubscriptions(query: SubscriptionQuery) {
  const userId = useUserId()
  const params = page(query)
  appendFilter(params, "plan", query.plan)
  appendFilter(params, "state", query.state)
  appendFilter(params, "q", query.q)
  return useQuery({
    queryKey: adminBillingKeys.subscriptions(userId, query),
    queryFn: () => http.get<AdminPage<SubscriptionRow>>(`/api/admin/billing/subscriptions?${params}`),
    // Paging a wide table should scroll, not blank out and re-measure.
    placeholderData: keepPreviousData,
    ...READ_ONCE,
  })
}

export function useAdminOrders(query: OrderQuery) {
  const userId = useUserId()
  const params = page(query)
  appendFilter(params, "provider", query.provider)
  appendFilter(params, "status", query.status)
  appendFilter(params, "kind", query.kind)
  // Calendar days, not datetimes: a full ISO timestamp answers 422.
  appendFilter(params, "from", query.from)
  appendFilter(params, "to", query.to)
  appendFilter(params, "q", query.q)
  return useQuery({
    queryKey: adminBillingKeys.orders(userId, query),
    queryFn: () => http.get<AdminPage<AdminOrderRow>>(`/api/admin/billing/orders?${params}`),
    placeholderData: keepPreviousData,
    ...READ_ONCE,
  })
}

export function useWorkspaceBilling(workspaceId: string) {
  const userId = useUserId()
  return useQuery({
    queryKey: adminBillingKeys.workspace(userId, workspaceId),
    queryFn: () =>
      http.get<WorkspaceBillingDetail>(
        `/api/admin/billing/workspaces/${encodeURIComponent(workspaceId)}`,
      ),
    enabled: Boolean(workspaceId),
    ...READ_ONCE,
  })
}
