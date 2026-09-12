// The viewer's only transport. Every call must stay under the admin trajectory
// API and be a read, except creating an export — no chat, tool, permission,
// question, cancel, sandbox, attachment or ticket endpoint is reachable here.
import { beforeEach, describe, expect, it, vi } from "vitest"
import { http, requestBlob } from "@/shared/api/http"
import { queryString, trajectoryApi, TRAJECTORY_API } from "./endpoints"

vi.mock("@/shared/api/http", () => ({
  http: {
    get: vi.fn(async () => ({})),
    post: vi.fn(async () => ({})),
    put: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  },
  requestBlob: vi.fn(async () => ({ blob: new Blob(), filename: null })),
}))

const signal = new AbortController().signal

beforeEach(() => vi.clearAllMocks())

describe("admin trajectory endpoints", () => {
  it("reads everything with GET under the admin trajectory API", async () => {
    await trajectoryApi.listSessions(
      { sort: "last_activity_asc", cursor: "abc_-", include_unrecorded: true },
      signal,
    )
    await trajectoryApi.header("ses/1", "12", signal)
    await trajectoryApi.records(
      "ses/1",
      { throughSeq: "12", before: "cur", kind: "tool", agentId: "a" },
      signal,
    )
    await trajectoryApi.record("ses/1", "tool:call a", "12", signal)
    await trajectoryApi.events("ses/1", { afterSeq: "9007199254740993", limit: 500 }, signal)
    await trajectoryApi.checkpoint("ses/1", undefined, signal)
    await trajectoryApi.search("ses/1", { q: "x y", throughSeq: "12" }, signal)
    await trajectoryApi.exportStatus("ses/1", "exp_1", signal)
    await trajectoryApi.payload("ses/1", "pay_1", "12", signal)
    await trajectoryApi.downloadExport("ses/1", "exp_1", signal)

    const gets = vi.mocked(http.get).mock.calls.map(([path]) => path)
    const blobs = vi.mocked(requestBlob).mock.calls.map(([path]) => path)
    for (const path of [...gets, ...blobs]) expect(path.startsWith(`${TRAJECTORY_API}/sessions`)).toBe(true)
    expect(http.post).not.toHaveBeenCalled()
    expect(http.put).not.toHaveBeenCalled()
    expect(http.patch).not.toHaveBeenCalled()
    expect(http.delete).not.toHaveBeenCalled()
    const list = new URL(gets[0], "http://api.test")
    expect(list.pathname).toBe(`${TRAJECTORY_API}/sessions`)
    expect(Object.fromEntries(list.searchParams)).toEqual({
      sort: "last_activity_asc",
      cursor: "abc_-",
      include_unrecorded: "true",
    })
    expect(gets).toContain(`${TRAJECTORY_API}/sessions/ses%2F1/records/tool%3Acall%20a?through_seq=12`)
    expect(gets).toContain(
      `${TRAJECTORY_API}/sessions/ses%2F1/events?after_seq=9007199254740993&limit=500&include_data=true`,
    )
    expect(gets.some((path) => path.includes("ticket"))).toBe(false)
    // Every read is cancellable when the target changes or access is lost.
    for (const call of vi.mocked(http.get).mock.calls) expect(call[1]).toEqual({ signal })
  })

  it("has exactly one write: creating an export at a fixed watermark", async () => {
    await trajectoryApi.createExport("ses_b", "34", signal)
    expect(http.post).toHaveBeenCalledWith(
      `${TRAJECTORY_API}/sessions/ses_b/export`,
      { through_seq: "34" },
      { signal },
    )
  })

  it("omits empty parameters", () => {
    expect(queryString({ a: "", b: undefined, c: null, d: false, e: "0" })).toBe("?e=0")
    expect(queryString({})).toBe("")
  })
})
