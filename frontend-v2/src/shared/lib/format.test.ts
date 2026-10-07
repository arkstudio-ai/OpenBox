import { describe, expect, it, vi } from "vitest"

vi.mock("@/shared/i18n", () => ({ default: { language: "en-US" } }))

const { formatAmount, formatBytes, formatClock, formatDuration, formatTokens, formatYuan } =
  await import("./format")

describe("formatBytes", () => {
  it("handles zero", () => {
    expect(formatBytes(0)).toBe("0 B")
  })
  it("scales units", () => {
    expect(formatBytes(1024)).toBe("1 KB")
    expect(formatBytes(2_516_582)).toBe("2.4 MB")
  })
  it("caps at GB", () => {
    expect(formatBytes(5 * 1024 ** 4)).toMatch(/GB$/)
  })
})

describe("formatDuration", () => {
  it("sub-10s keeps one decimal", () => {
    expect(formatDuration(4.63)).toBe("4.6s")
  })
  it("sub-minute rounds", () => {
    expect(formatDuration(42.4)).toBe("42s")
  })
  it("minutes split", () => {
    expect(formatDuration(96)).toBe("1m 36s")
  })
})

describe("formatTokens", () => {
  it("keeps small numbers", () => {
    expect(formatTokens(842)).toBe("842")
  })
  it("abbreviates thousands", () => {
    expect(formatTokens(12_400)).toBe("12.4k")
  })
})

describe("formatClock", () => {
  it("pads minutes and seconds", () => {
    expect(formatClock(0)).toBe("00:00")
    expect(formatClock(134.9)).toBe("02:14")
  })
  it("keeps counting minutes past the hour", () => {
    expect(formatClock(3725)).toBe("62:05")
  })
})

describe("money", () => {
  it("fixes the decimals of Decimal strings", () => {
    expect(formatAmount("0.003500", 4)).toBe("0.0035")
    expect(formatYuan("0.0035")).toBe("¥0.0035")
    expect(formatYuan("0.000123", 6)).toBe("¥0.000123")
  })
  it("reads garbage as zero", () => {
    expect(formatAmount("n/a", 4)).toBe("0.0000")
  })
})
