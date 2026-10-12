import { describe, expect, it } from "vitest"
import {
  EMPTY_TURNS_PARAMS,
  EMPTY_USERS_PARAMS,
  isAbnormalOnly,
  isLegacyListSearch,
  nextPage,
  parseTurnsParams,
  parseUsersParams,
  previousPage,
  serializeTurnsParams,
  serializeUsersParams,
  windowStart,
  withFilters,
} from "./params"
import { ABNORMAL_STATUSES } from "./types"

describe("lite URL params", () => {
  it("round-trips the user list filters and omits defaults", () => {
    expect(serializeUsersParams(EMPTY_USERS_PARAMS)).toBe("")
    const params = {
      ...EMPTY_USERS_PARAMS,
      q: " 张店长 ",
      window: "30d" as const,
      failures: true,
      active: true,
    }
    const parsed = parseUsersParams(new URLSearchParams(serializeUsersParams(params)))
    expect(parsed).toEqual({ ...params, q: "张店长" })
    expect(parseUsersParams(new URLSearchParams("window=yesterday&failures=yes")).window).toBe("7d")
    expect(parseUsersParams(new URLSearchParams("failures=yes")).failures).toBe(false)
  })

  it("round-trips the turn list filters, keeping only known statuses", () => {
    expect(serializeTurnsParams(EMPTY_TURNS_PARAMS)).toBe("")
    const parsed = parseTurnsParams(
      new URLSearchParams("status=failed,done,waiting,failed&session=s1&q=发视频&window=7d"),
    )
    expect(parsed).toEqual({
      ...EMPTY_TURNS_PARAMS,
      statuses: ["failed", "waiting"],
      session: "s1",
      q: "发视频",
      window: "7d",
    })
    expect(parseTurnsParams(new URLSearchParams(serializeTurnsParams(parsed)))).toEqual(parsed)
    expect(parseTurnsParams(new URLSearchParams("window=1y")).window).toBe("30d")
  })

  it("pages with an opaque cursor trail and restarts on a damaged one", () => {
    const first = withFilters(EMPTY_USERS_PARAMS, { q: "a" })
    const second = nextPage(first, "cursorB")
    const third = nextPage(second, "cursorC")
    expect(third).toMatchObject({ cursor: "cursorC", trail: [null, "cursorB"] })
    const reparsed = parseUsersParams(new URLSearchParams(serializeUsersParams(third)))
    expect(reparsed).toEqual(third)
    expect(previousPage(reparsed)).toEqual(second)
    expect(previousPage(first)).toBe(first)
    expect(parseUsersParams(new URLSearchParams("q=a&cursor=not%20base64&trail=.")).cursor).toBeNull()
    expect(parseUsersParams(new URLSearchParams("q=a&cursor=ok&trail=bad%2Fcursor")).trail).toEqual([])
    // Changing a filter drops the cursors: they belong to another list.
    expect(withFilters(third, { failures: true })).toMatchObject({ cursor: null, trail: [] })
  })

  it("knows when exactly the abnormal statuses are selected", () => {
    expect(isAbnormalOnly([...ABNORMAL_STATUSES])).toBe(true)
    expect(isAbnormalOnly([...ABNORMAL_STATUSES].reverse())).toBe(true)
    expect(isAbnormalOnly(["failed"])).toBe(false)
    expect(isAbnormalOnly([...ABNORMAL_STATUSES, "completed"])).toBe(false)
  })

  it("starts a window at local midnight so the statistics are stable within a day", () => {
    const now = new Date(2026, 9, 12, 15, 30)
    expect(windowStart("today", now)).toBe(new Date(2026, 9, 12).toISOString())
    expect(windowStart("7d", now)).toBe(new Date(2026, 9, 6).toISOString())
    expect(windowStart("30d", now)).toBe(new Date(2026, 8, 13).toISOString())
    expect(windowStart("all", now)).toBeUndefined()
  })

  it("recognises a bookmark of the professional session list by its filter keys", () => {
    expect(isLegacyListSearch(new URLSearchParams("owner=zhang"))).toBe(true)
    expect(isLegacyListSearch(new URLSearchParams("sort=last_activity_asc&cursor=x"))).toBe(true)
    expect(isLegacyListSearch(new URLSearchParams("unrecorded=1"))).toBe(true)
    expect(isLegacyListSearch(new URLSearchParams("q=zhang&window=30d"))).toBe(false)
    expect(isLegacyListSearch(new URLSearchParams(""))).toBe(false)
  })
})
