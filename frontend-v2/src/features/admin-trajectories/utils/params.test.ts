import { describe, expect, it } from "vitest"
import {
  EMPTY_DETAIL_PARAMS,
  EMPTY_LIST_PARAMS,
  MAX_CURSOR,
  nextPage,
  parseDetailParams,
  parseListParams,
  previousPage,
  serializeDetailParams,
  serializeListParams,
  toApiParams,
  withFilters,
} from "./params"

// A realistic server cursor: base64url of [filters, microsecond timestamp, session id],
// well over the 200 characters free-text fields are trimmed to.
const LONG_CURSOR = `${"W1siYWRtaW4iLG51bGwsInF1ZXJ5Il0sIjIwMjYtMDktMTFUMDg6MDA6MDAuMTIzNDU2KzAwOjAwIiwic2Vzc2lvbl9hIl0".repeat(4)}-_x`

const parse = (query: string) => parseListParams(new URLSearchParams(query))

describe("session list URL state", () => {
  it("round-trips filters, sort, cursor and trail exactly", () => {
    const params = nextPage(
      nextPage(
        withFilters(EMPTY_LIST_PARAMS, { userQuery: "lin", recording: "gap", sort: "last_activity_asc" }),
        "c1",
      ),
      LONG_CURSOR,
    )
    expect(LONG_CURSOR.length).toBeGreaterThan(200)
    const parsed = parse(serializeListParams(params))
    expect(parsed).toEqual(params)
    expect(parsed.cursor).toBe(LONG_CURSOR)
    expect(parsed.trail).toEqual([null, "c1"])
    expect(previousPage(parsed).cursor).toBe("c1")
  })

  it("sends the sort to the server rather than sorting a page locally", () => {
    expect(toApiParams(parse("sort=last_activity_asc")).sort).toBe("last_activity_asc")
    expect(toApiParams(parse("")).sort).toBe("last_activity_desc")
    expect(parse("sort=title").sort).toBe("last_activity_desc")
    expect(serializeListParams(parse("sort=last_activity_desc"))).toBe("")
  })

  it("restarts paging when filters or sort change, because cursors are bound to them", () => {
    const paged = nextPage(EMPTY_LIST_PARAMS, "abc")
    expect(withFilters(paged, { sort: "last_activity_asc" })).toMatchObject({ cursor: null, trail: [] })
    expect(withFilters(paged, { q: "x" })).toMatchObject({ cursor: null, trail: [] })
  })

  it("refuses damaged or oversized cursors instead of trimming them", () => {
    expect(parse("q=keep&cursor=not%20base64!")).toMatchObject({ q: "keep", cursor: null, trail: [] })
    expect(parse(`cursor=${"a".repeat(MAX_CURSOR + 1)}`).cursor).toBeNull()
    expect(parse("cursor=abc&trail=.,bad%20entry")).toMatchObject({ cursor: null, trail: [] })
  })

  it("keeps free-text filters bounded", () => {
    expect(parse(`q=${"x".repeat(500)}`).q).toHaveLength(200)
  })
})

describe("detail URL state", () => {
  it("preserves the full list query, including a long cursor, for the way back", () => {
    const list = serializeListParams(
      nextPage(withFilters(EMPTY_LIST_PARAMS, { sort: "last_activity_asc" }), LONG_CURSOR),
    )
    const detail = parseDetailParams(
      new URLSearchParams(
        serializeDetailParams({
          ...EMPTY_DETAIL_PARAMS,
          back: list,
          at: "9007199254740993",
          record: "tool:call_a",
        }),
      ),
    )
    expect(detail.back).toBe(list)
    expect(parse(detail.back).cursor).toBe(LONG_CURSOR)
    expect(detail.at).toBe("9007199254740993")
    expect(detail.record).toBe("tool:call_a")
  })

  it("drops a replay position that is not a sequence number", () => {
    expect(parseDetailParams(new URLSearchParams("at=12abc")).at).toBeNull()
  })
})
