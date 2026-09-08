// Query keys carry the user id (ENGINEERING_SPEC §7.2): the console is only
// reachable by admins, but a logout/login as a different admin must not serve
// the previous operator's cached page.
import type { OrderQuery, SubscriptionQuery } from "@/features/admin-billing/types"

export const adminBillingKeys = {
  all: (userId: string) => ["admin-billing", userId] as const,
  subscriptions: (userId: string, query: SubscriptionQuery) =>
    [
      "admin-billing",
      userId,
      "subscriptions",
      query.plan,
      query.state,
      query.q,
      query.offset,
      query.limit,
    ] as const,
  orders: (userId: string, query: OrderQuery) =>
    [
      "admin-billing",
      userId,
      "orders",
      query.provider,
      query.status,
      query.kind,
      query.from,
      query.to,
      query.q,
      query.offset,
      query.limit,
    ] as const,
  workspace: (userId: string, workspaceId: string) =>
    ["admin-billing", userId, "workspace", workspaceId] as const,
}
