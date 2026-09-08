import { describe, expect, it } from "vitest"
import { formatFen } from "./money"

describe("formatFen", () => {
  // The boundaries that catch a wrong divisor or a dropped trailing zero:
  // nothing, a bare fen, just under a yuan, and a thousand yuan.
  it("renders fen as two decimals in the operator's locale", () => {
    expect(formatFen(0, "CNY", "zh-CN")).toBe("¥0.00")
    expect(formatFen(10, "CNY", "zh-CN")).toBe("¥0.10")
    expect(formatFen(999, "CNY", "zh-CN")).toBe("¥9.99")
    expect(formatFen(100000, "CNY", "zh-CN")).toBe("¥1,000.00")
  })

  it("disambiguates the currency for an English reader", () => {
    expect(formatFen(0, "CNY", "en-US")).toBe("CN¥0.00")
    expect(formatFen(999, "CNY", "en-US")).toBe("CN¥9.99")
    expect(formatFen(100000, "USD", "en-US")).toBe("$1,000.00")
  })

  it("keeps both decimals on a round amount", () => {
    // A price shown as "¥1" reads as a rounding bug to whoever reconciles it.
    expect(formatFen(100, "CNY", "zh-CN")).toBe("¥1.00")
    expect(formatFen(1, "CNY", "zh-CN")).toBe("¥0.01")
  })

  it("shows a negative amount rather than dropping the sign", () => {
    expect(formatFen(-999, "CNY", "zh-CN")).toBe("-¥9.99")
  })

  it("falls back to the raw code when Intl rejects the currency", () => {
    // A malformed code must not take the whole order list down.
    expect(formatFen(999, "not-a-code", "en-US")).toBe("not-a-code 9.99")
  })
})
