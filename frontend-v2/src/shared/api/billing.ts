import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useEffect } from "react"
import { http } from "./http"
import { useAuthStore } from "./auth-store"
import { useWorkspaceStore } from "./workspace-store"

export interface CreditBalance {
  workspace_id: string
  balance: string
  mode: "off" | "shadow" | "enforce"
}

export interface BillingSummary {
  total_tokens: number
  total_credits: string
  charged_credits: string
  historical_count: number
  unpriced_count: number
}

export interface UsageDateFilter {
  date_from?: string
  date_to?: string
  tz?: string
}

function withUsageDates(path: string, dates: UsageDateFilter): string {
  const params = new URLSearchParams()
  for (const key of ["date_from", "date_to", "tz"] as const) {
    if (dates[key]) params.set(key, dates[key])
  }
  const query = params.toString()
  return query ? `${path}${path.includes("?") ? "&" : "?"}${query}` : path
}

export interface UsageEntry {
  id: string
  session_id: string
  session_title: string
  session_available: boolean
  model_id: string
  kind: string
  tokens: {
    input?: number
    output?: number
    cache?: number
    // Media events (kind video_compose / video_generate) carry duration instead of tokens.
    duration_sec?: number | null
    minutes_billed?: number
    seconds_billed?: number
    tier?: string
    resolution?: string
    images?: number
  items?: number
  }
  total_tokens: number
  credits: string | null
  status: "charged" | "shadow" | "historical" | "unpriced" | "pending" | "unreported"
  created_at: string
  pricing_version: string
}

export interface Page<T> {
  items: T[]
  total: number
  page: number
  page_size: number
  total_pages: number
}

export interface PaymentOrder {
  id: string
  provider: string
  credits: string
  status: "pending" | "paid" | "cancelled"
  checkout_url: string | null
  created_at: string
  paid_at: string | null
  cancelled_at?: string | null
  cancellation_reason?: "trade_not_created" | "gateway_closed" | null
  reconcile_required?: boolean
  kind: "topup" | "subscription"
  amount_fen: number
  currency: "CNY"
  plan_id: "free" | "pro" | "max" | null
  cycle: BillingCycle | null
  starts_at: string | null
  ends_at: string | null
}

export interface PaymentProvider {
  id: string
  name: string
  confirmation_mode?: "callback" | "query"
  supports_status_query?: boolean
  supports_cancel?: boolean
  refresh_checkout?: boolean
}

export type BillingCycle = "monthly" | "yearly"
export interface BillingPlan {
  id: "free" | "pro" | "max"
  prices_fen: Record<BillingCycle, number>
  credits: string
  credit_period: "weekly" | "monthly"
  recommended: boolean
}
export interface TopupRules {
  min_amount_fen: number
  max_amount_fen: number
  presets_fen: number[]
  credits_per_yuan: "1"
}
export interface BillingPlans {
  version: string
  currency: "CNY"
  plans: BillingPlan[]
  topup: TopupRules
}
export interface SubscriptionTerm {
  plan_id: BillingPlan["id"]
  cycle: BillingCycle | null
  starts_at: string | null
  ends_at: string | null
}
export interface BillingSubscription extends SubscriptionTerm {
  credits: string
  credit_period: "weekly" | "monthly"
  next_grant_at: string
  topup_allowed: boolean
  can_manage: boolean
  queued: SubscriptionTerm[]
}
export type CreatePaymentOrder = { provider: string; request_key: string } & (
  | { kind?: "topup"; amount_fen: number }
  | { kind: "subscription"; plan_id: "pro" | "max"; cycle: BillingCycle }
)

function useBillingScope() {
  const userId = useAuthStore((s) => s.user?.id)
  const workspaceId = useWorkspaceStore((s) => s.currentId)
  return {
    key: ["billing", userId, workspaceId] as const,
    enabled: Boolean(userId && workspaceId),
    options: { headers: { "X-Workspace-Id": workspaceId ?? "" } },
  }
}

export function useCreditBalance() {
  const scope = useBillingScope()
  return useQuery({
    queryKey: [...scope.key, "balance"],
    enabled: scope.enabled,
    queryFn: () => http.get<CreditBalance>("/api/billing/balance", scope.options),
    refetchInterval: 15_000,
  })
}

export function useBillingSummary(dates: UsageDateFilter = {}) {
  const scope = useBillingScope()
  return useQuery({
    queryKey: [...scope.key, "summary", dates],
    enabled: scope.enabled,
    queryFn: () => http.get<BillingSummary>(withUsageDates("/api/billing/summary", dates), scope.options),
    refetchInterval: 15_000,
  })
}

export function useUsageEvents(page: number, pageSize = 20, dates: UsageDateFilter = {}) {
  const scope = useBillingScope()
  return useQuery({
    queryKey: [...scope.key, "usage", page, pageSize, dates],
    enabled: scope.enabled,
    queryFn: () =>
      http.get<Page<UsageEntry>>(
        withUsageDates(`/api/billing/usage?page=${page}&page_size=${pageSize}`, dates),
        scope.options,
      ),
    refetchInterval: 15_000,
  })
}

export function usePaymentProviders() {
  const scope = useBillingScope()
  return useQuery({
    queryKey: [...scope.key, "providers"],
    enabled: scope.enabled,
    queryFn: () => http.get<{ items: PaymentProvider[] }>("/api/billing/providers", scope.options),
  })
}

export function useBillingPlans() {
  const scope = useBillingScope()
  return useQuery({
    queryKey: [...scope.key, "plans"],
    enabled: scope.enabled,
    queryFn: () => http.get<BillingPlans>("/api/billing/plans", scope.options),
  })
}

export function useBillingSubscription() {
  const scope = useBillingScope()
  return useQuery({
    queryKey: [...scope.key, "subscription"],
    enabled: scope.enabled,
    queryFn: () => http.get<BillingSubscription>("/api/billing/subscription", scope.options),
    refetchInterval: 15_000,
  })
}

export function useCreatePaymentOrder() {
  const scope = useBillingScope()
  const client = useQueryClient()
  return useMutation({
    mutationFn: (body: CreatePaymentOrder) =>
      http.post<PaymentOrder>("/api/billing/orders", body, scope.options),
    // Even a failed response may have persisted an order that can be resumed.
    onSettled: () => void client.invalidateQueries({ queryKey: [...scope.key, "orders"] }),
  })
}

export function useContinuePaymentOrder() {
  const scope = useBillingScope()
  const client = useQueryClient()
  return useMutation({
    mutationFn: (orderId: string) =>
      http.post<PaymentOrder>(`/api/billing/orders/${orderId}/checkout`, undefined, scope.options),
    onSuccess: (order) => {
      client.setQueryData([...scope.key, "order", order.id], order)
      void client.invalidateQueries({ queryKey: scope.key })
    },
  })
}

export function useRefreshPaymentOrder() {
  const scope = useBillingScope()
  const client = useQueryClient()
  return useMutation({
    mutationFn: (orderId: string) =>
      http.post<PaymentOrder>(`/api/billing/orders/${orderId}/refresh`, undefined, scope.options),
    onSuccess: (order) => {
      client.setQueryData([...scope.key, "order", order.id], order)
      void client.invalidateQueries({ queryKey: scope.key })
    },
  })
}

export function useCancelPaymentOrder() {
  const scope = useBillingScope()
  const client = useQueryClient()
  return useMutation({
    mutationFn: (orderId: string) =>
      http.post<PaymentOrder>(`/api/billing/orders/${orderId}/cancel`, undefined, scope.options),
    onSuccess: (order) => {
      client.setQueryData([...scope.key, "order", order.id], order)
      void client.invalidateQueries({ queryKey: scope.key })
    },
  })
}

export function usePaymentStatusSync(orders: PaymentOrder[] = [], providers: PaymentProvider[] = []) {
  const scope = useBillingScope()
  const client = useQueryClient()
  const ids = orders
    .filter(
      (order) =>
        (order.status === "pending" || order.reconcile_required) &&
        providers.some((provider) => provider.id === order.provider && provider.supports_status_query),
    )
    .map((order) => order.id)
    .sort()
  const enabled = scope.enabled && ids.length > 0
  const query = useQuery({
    queryKey: [...scope.key, "payment-status", ids],
    enabled,
    retry: false,
    refetchOnMount: "always",
    refetchInterval: 15_000,
    queryFn: async () => {
      const remaining = [...ids]
      let changed = false
      let failed = false
      // Bound upstream traffic when the order list contains many old attempts.
      await Promise.all(
        Array.from({ length: Math.min(3, ids.length) }, async () => {
          let id: string | undefined
          while ((id = remaining.shift())) {
            try {
              const order = await http.post<PaymentOrder>(
                `/api/billing/orders/${id}/refresh`,
                undefined,
                scope.options,
              )
              client.setQueryData([...scope.key, "order", id], order)
              const previous = orders.find((item) => item.id === id)
              if (
                previous?.status !== order.status ||
                previous?.reconcile_required !== order.reconcile_required
              )
                changed = true
            } catch {
              failed = true
            }
          }
        }),
      )
      if (changed) {
        for (const resource of ["orders", "balance", "subscription"]) {
          void client.invalidateQueries({ queryKey: [...scope.key, resource] })
        }
      }
      return { failed }
    },
  })
  const { refetch } = query
  useEffect(() => {
    if (!enabled) return
    const sync = () => {
      if (document.visibilityState === "visible") void refetch({ cancelRefetch: false })
    }
    window.addEventListener("focus", sync)
    window.addEventListener("pageshow", sync)
    return () => {
      window.removeEventListener("focus", sync)
      window.removeEventListener("pageshow", sync)
    }
  }, [enabled, refetch])
  return query
}

export interface PaymentOrderFilters extends UsageDateFilter {
  status?: PaymentOrder["status"]
  provider?: string
  order_id?: string
}

export function usePaymentOrders(page: number, filters: PaymentOrderFilters = {}, pageSize = 10) {
  const scope = useBillingScope()
  const client = useQueryClient()
  const query = useQuery({
    queryKey: [...scope.key, "orders", page, filters, pageSize],
    enabled: scope.enabled,
    queryFn: () => {
      const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) })
      for (const [key, value] of Object.entries(filters)) if (value) params.set(key, value)
      return http.get<Page<PaymentOrder>>(`/api/billing/orders?${params}`, scope.options)
    },
    refetchInterval: (q) =>
      q.state.data?.items.some((order) => order.status === "pending") ? 5_000 : 30_000,
  })
  const paidOrders = query.data?.items
    .filter((order) => order.status === "paid")
    .map((order) => order.id)
    .join(",")
  const [billing, userId, workspaceId] = scope.key
  useEffect(() => {
    if (paidOrders) {
      void client.invalidateQueries({ queryKey: [billing, userId, workspaceId, "balance"] })
      void client.invalidateQueries({ queryKey: [billing, userId, workspaceId, "subscription"] })
    }
  }, [paidOrders, client, billing, userId, workspaceId])
  return query
}

export function usePaymentOrder(orderId: string | undefined) {
  const scope = useBillingScope()
  const client = useQueryClient()
  const query = useQuery({
    queryKey: [...scope.key, "order", orderId],
    enabled: scope.enabled && Boolean(orderId),
    queryFn: () => http.get<PaymentOrder>(`/api/billing/orders/${orderId}`, scope.options),
    refetchInterval: (q) => (q.state.data?.status === "pending" ? 2_000 : false),
  })
  const paid = query.data?.status === "paid"
  useEffect(() => {
    if (paid) void client.invalidateQueries({ queryKey: ["billing"] })
  }, [paid, client])
  return query
}
