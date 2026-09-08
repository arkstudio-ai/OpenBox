// Wire shapes of `backend/api/admin_billing.py`. Every timestamp is a UTC ISO
// string and every Decimal is a string: money is never a JS number here, and
// the only division this feature performs is `amount_fen / 100` in lib/money.

export interface UserBrief {
  id: string
  username: string
  email: string | null
}

export interface WorkspaceBrief {
  id: string
  name: string
  kind: string
}

/** A page of any admin list; `total` counts rows before `offset`/`limit`. */
export interface AdminPage<T> {
  items: T[]
  total: number
  offset: number
  limit: number
}

export type SubscriptionState = "active" | "expired" | "free"
export type OrderStatus = "pending" | "paid" | "cancelled"
export type OrderKind = "topup" | "subscription"

export interface SubscriptionRow {
  workspace: WorkspaceBrief
  /** Null once the owning account is gone; the workspace row outlives it. */
  owner: UserBrief | null
  plan_id: string
  cycle: string | null
  starts_at: string | null
  ends_at: string | null
  state: SubscriptionState
  queued_count: number
  balance: string | null
  last_paid_at: string | null
}

/** One `payment_orders` row as the operator view returns it — no checkout URL. */
export interface AdminOrder {
  id: string
  workspace_id: string
  user_id: string
  user: UserBrief | null
  provider: string
  kind: OrderKind
  amount_fen: number
  currency: string
  credits: string | null
  status: OrderStatus
  plan_id: string | null
  cycle: string | null
  provider_order_id: string | null
  created_at: string
  paid_at: string | null
  cancelled_at: string | null
  cancellation_reason: string | null
}

export interface AdminOrderRow extends AdminOrder {
  /**
   * Only the cross-tenant list carries it — the workspace detail page already
   * knows whose orders it is showing. Optional so both shapes can share one
   * column set instead of one being cast into the other.
   */
  workspace_name?: string | null
}

export interface SubscriptionTerm {
  order_id: string
  plan_id: string
  cycle: string | null
  starts_at: string | null
  ends_at: string | null
}

export interface LedgerEntry {
  id: string
  kind: string
  amount: string | null
  balance_after: string | null
  reference_id: string | null
  created_at: string | null
}

export interface UsageBucket {
  status: string
  events: number
  total_tokens: number
  credits: string | null
}

export interface WorkspaceBillingDetail {
  workspace: WorkspaceBrief & {
    owner_user_id: string | null
    plan_id: string | null
    created_at: string | null
    is_deleted: boolean
    deleted_at: string | null
  }
  owner: UserBrief | null
  member_count: number
  balance: string | null
  plan_id: string
  subscription: SubscriptionTerm | null
  /** Terms that start in the future, ascending. */
  queued: SubscriptionTerm[]
  /** Every term, newest first; includes the current and the queued ones. */
  history: SubscriptionTerm[]
  orders: AdminOrder[]
  ledger: LedgerEntry[]
  usage: {
    since: string | null
    days: number
    items: UsageBucket[]
  }
}

export interface SubscriptionQuery {
  plan: string
  state: string
  q: string
  offset: number
  limit: number
}

/** Everything the order list filters on except the page cursor. */
export interface OrderFilterDraft {
  provider: string
  status: string
  kind: string
  /** Calendar days, `YYYY-MM-DD`, both ends inclusive and read as UTC. */
  from: string
  to: string
  q: string
}

export interface OrderQuery extends OrderFilterDraft {
  offset: number
  limit: number
}
