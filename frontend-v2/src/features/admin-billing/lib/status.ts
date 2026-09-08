// State → tone. Kept out of the components so the mapping is testable and so
// the two lists can never disagree about what "expired" looks like (§9.2:
// callers map a status to a meaning, never to a colour).
import type { StatusTone } from "@/shared/ui/StatusPill"
import type { OrderStatus, SubscriptionState } from "@/features/admin-billing/types"

const SUBSCRIPTION_TONES: Record<SubscriptionState, StatusTone> = {
  active: "ok",
  // Warn, not danger: a lapsed term is a renewal opportunity, not a failure.
  expired: "warn",
  free: "muted",
}

const ORDER_TONES: Record<OrderStatus, StatusTone> = {
  paid: "ok",
  pending: "warn",
  cancelled: "danger",
}

/** Unknown states stay legible rather than crashing on an index miss. */
export function subscriptionTone(state: SubscriptionState): StatusTone {
  return SUBSCRIPTION_TONES[state] ?? "muted"
}

export function orderTone(status: OrderStatus): StatusTone {
  return ORDER_TONES[status] ?? "muted"
}
