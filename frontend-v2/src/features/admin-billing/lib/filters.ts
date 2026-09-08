// URL state is always strings (`useUrlState`), so "no filter" needs a token the
// address bar can hold. It is deliberately not "" — an empty `q` and an empty
// `plan` would otherwise be indistinguishable when building the request.
export const ALL = "all"

/** Rows per page. Well under the server's hard cap of 200. */
export const PAGE_SIZE = 25

// Mirrors `admin_billing.DETAIL_ORDER_LIMIT` / `DETAIL_LEDGER_LIMIT`. The
// detail response is not paginated, so these are only used to tell the reader
// that a long history is truncated rather than empty below this point.
export const DETAIL_ORDER_LIMIT = 100
export const DETAIL_LEDGER_LIMIT = 50

/** Plan ids the server accepts; anything else answers 422. */
export const PLAN_IDS = ["free", "pro", "max"] as const

export const SUBSCRIPTION_STATES = ["active", "expired", "free"] as const
export const ORDER_STATUSES = ["pending", "paid", "cancelled"] as const
export const ORDER_KINDS = ["topup", "subscription"] as const

/** A hand-edited `?offset=` must not send `NaN` or a negative to the server. */
export function parseOffset(value: string): number {
  const offset = Number(value)
  return Number.isFinite(offset) && offset > 0 ? Math.floor(offset) : 0
}

/** Drops the "all" sentinel and blank values so no filter is sent as `?plan=`. */
export function appendFilter(params: URLSearchParams, key: string, value: string): void {
  const trimmed = value.trim()
  if (!trimmed || trimmed === ALL) return
  params.set(key, trimmed)
}
