import { describe, expect, it } from "vitest"
import type { OrderStatus, SubscriptionState } from "@/features/admin-billing/types"
import { orderTone, subscriptionTone } from "./status"

describe("subscriptionTone", () => {
  it("maps each subscription state to its meaning", () => {
    expect(subscriptionTone("active")).toBe("ok")
    // Not "danger": a lapsed term is a renewal to chase, not an incident.
    expect(subscriptionTone("expired")).toBe("warn")
    expect(subscriptionTone("free")).toBe("muted")
  })

  it("stays legible if the server grows a state the UI has not learned", () => {
    expect(subscriptionTone("trialing" as SubscriptionState)).toBe("muted")
  })
})

describe("orderTone", () => {
  it("maps each order status to its meaning", () => {
    expect(orderTone("paid")).toBe("ok")
    expect(orderTone("pending")).toBe("warn")
    expect(orderTone("cancelled")).toBe("danger")
  })

  it("stays legible on an unknown status", () => {
    expect(orderTone("refunded" as OrderStatus)).toBe("muted")
  })
})
