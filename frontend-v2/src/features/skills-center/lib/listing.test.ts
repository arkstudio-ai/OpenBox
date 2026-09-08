import { describe, expect, it } from "vitest"
import { canWithdraw, isResubmission, listingChipFor } from "./listing"

// Two independent facts decide one chip. The pairs that matter are the ones
// where the author and the operator disagree: a withdrawn package that was
// still listed, and a published one an operator has since pulled.
describe("listingChipFor", () => {
  it("maps every (status, listing) pair the author can be in", () => {
    expect(listingChipFor("published", "pending")).toBe("pending")
    expect(listingChipFor("published", "listed")).toBe("listed")
    expect(listingChipFor("published", "rejected")).toBe("rejected")
    expect(listingChipFor("published", "delisted")).toBe("delisted")
    expect(listingChipFor("withdrawn", "listed")).toBe("withdrawn")
  })

  it("lets the author's withdrawal win over the operator's shelf state", () => {
    // The store is not showing it either way; saying "listed" would be a lie
    // the author cannot act on.
    expect(listingChipFor("withdrawn", "pending")).toBe("withdrawn")
    expect(listingChipFor("withdrawn", "rejected")).toBe("withdrawn")
  })

  it("says nothing about a draft that was never submitted", () => {
    expect(listingChipFor("unpublished", null)).toBeNull()
    expect(listingChipFor(null, null)).toBeNull()
    expect(listingChipFor(undefined, undefined)).toBeNull()
  })

  it("treats a missing listing as listed, for a backend that predates shelves", () => {
    expect(listingChipFor("published", null)).toBe("listed")
    expect(listingChipFor("published", undefined)).toBe("listed")
  })
})

describe("isResubmission", () => {
  it("is true wherever a release already exists that somebody ruled on", () => {
    expect(isResubmission("rejected")).toBe(true)
    expect(isResubmission("delisted")).toBe(true)
    expect(isResubmission("withdrawn")).toBe(true)
  })

  it("is false for a first upload or a routine update", () => {
    expect(isResubmission(null)).toBe(false)
    expect(isResubmission("listed")).toBe(false)
    expect(isResubmission("pending")).toBe(false)
  })
})

describe("canWithdraw", () => {
  it("offers withdrawal for anything the store still holds", () => {
    expect(canWithdraw("pending")).toBe(true)
    expect(canWithdraw("listed")).toBe(true)
    expect(canWithdraw("rejected")).toBe(true)
    expect(canWithdraw("delisted")).toBe(true)
  })

  it("does not offer to withdraw twice, or to withdraw a draft", () => {
    expect(canWithdraw("withdrawn")).toBe(false)
    expect(canWithdraw(null)).toBe(false)
  })
})
