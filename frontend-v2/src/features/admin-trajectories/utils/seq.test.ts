import { describe, expect, it } from "vitest"
import {
  addSeq,
  cmpSeq,
  eqSeq,
  gtSeq,
  isSeq,
  lastIndexAtOrBefore,
  maxSeq,
  minSeq,
  SeqError,
  sortBySeq,
  toSeq,
} from "./seq"

// 2^53 + 1: the first integer Number cannot represent. Number("…993") === Number("…992").
const BEYOND_SAFE = "9007199254740993"
const JUST_BELOW = "9007199254740992"

describe("seq comparison beyond MAX_SAFE_INTEGER", () => {
  it("distinguishes neighbours that Number would merge", () => {
    expect(Number(BEYOND_SAFE)).toBe(Number(JUST_BELOW))
    expect(cmpSeq(BEYOND_SAFE, JUST_BELOW)).toBe(1)
    expect(eqSeq(BEYOND_SAFE, JUST_BELOW)).toBe(false)
  })

  it("orders by magnitude, not by string order", () => {
    expect(cmpSeq("9", "10")).toBe(-1)
    expect(cmpSeq("100", "99")).toBe(1)
    expect(gtSeq("18446744073709551615", "9223372036854775807")).toBe(true)
  })

  it("ignores leading zeros", () => {
    expect(cmpSeq("007", "7")).toBe(0)
    expect(toSeq("0042")).toBe("42")
    expect(maxSeq("0010", "9")).toBe("10")
    expect(minSeq("0010", "9")).toBe("9")
  })

  it("rejects values that are not decimal strings", () => {
    expect(isSeq("12a")).toBe(false)
    expect(isSeq("-1")).toBe(false)
    expect(isSeq(12)).toBe(false)
    expect(() => cmpSeq("1.5", "2")).toThrow(SeqError)
  })

  it("parses only safe numbers and non-negative bigints", () => {
    expect(toSeq(5)).toBe("5")
    expect(toSeq(2 ** 60)).toBeNull()
    expect(toSeq(BigInt(BEYOND_SAFE))).toBe(BEYOND_SAFE)
    expect(toSeq(-1)).toBeNull()
    expect(toSeq(null)).toBeNull()
  })
})

describe("seq arithmetic", () => {
  it("adds across the safe-integer boundary exactly", () => {
    expect(addSeq(JUST_BELOW, 1)).toBe(BEYOND_SAFE)
    expect(addSeq(BEYOND_SAFE, -1)).toBe(JUST_BELOW)
  })

  it("clamps at zero", () => {
    expect(addSeq("1", -5)).toBe("0")
  })
})

describe("sorted lookups", () => {
  const items = ["2", "9", "10", BEYOND_SAFE].map((seq) => ({ seq }))

  it("sorts by magnitude", () => {
    const shuffled = [items[3], items[0], items[2], items[1]]
    expect(sortBySeq(shuffled, (i) => i.seq).map((i) => i.seq)).toEqual(["2", "9", "10", BEYOND_SAFE])
  })

  it("finds the last item at or before a target", () => {
    expect(lastIndexAtOrBefore(items, "9", (i) => i.seq)).toBe(1)
    expect(lastIndexAtOrBefore(items, "1", (i) => i.seq)).toBe(-1)
    expect(lastIndexAtOrBefore(items, JUST_BELOW, (i) => i.seq)).toBe(2)
    expect(lastIndexAtOrBefore(items, BEYOND_SAFE, (i) => i.seq)).toBe(3)
  })
})
