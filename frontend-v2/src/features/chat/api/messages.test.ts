import { beforeEach, describe, expect, it, vi } from "vitest"
import { ApiError, http } from "@/shared/api/http"
import type { MessageWithParts } from "@/shared/types/api"
import { useStreamStore } from "../stores/stream"
import { fetchHistory, isHistoryCursorGone, loadOlderHistory, newestPersistedId, sendPromptAsync } from "./messages"

vi.mock("@/shared/api/http", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/shared/api/http")>()),
  http: { post: vi.fn(), get: vi.fn() },
}))

describe("sendPromptAsync", () => {
  beforeEach(() => vi.clearAllMocks())

  it("keeps explicit null distinct from a concrete reasoning strength", async () => {
    vi.mocked(http.post).mockResolvedValue({ ok: true })

    await sendPromptAsync("s1", {
      text: "first",
      model: "openai/gpt-5.4",
      variant: null,
      clientMessageId: "c1",
    })
    await sendPromptAsync("s1", {
      text: "second",
      model: "openai/gpt-5.4",
      variant: "high",
      clientMessageId: "c2",
    })

    expect(vi.mocked(http.post).mock.calls[0][1]).toMatchObject({ variant: null })
    expect(vi.mocked(http.post).mock.calls[1][1]).toMatchObject({ variant: "high" })
  })
})

describe("history pages", () => {
  beforeEach(() => {
    vi.resetAllMocks()
    useStreamStore.getState().clearMessages("s1")
  })
  const message = (id: string): MessageWithParts => ({
    id, session_id: "s1", role: "assistant", created_at: id, parts: [],
  })
  const held = () => useStreamStore.getState().messages.get("s1")?.map((m) => m.id)

  it("asks for the newest turns, the turns before a message, or a message and everything after it", async () => {
    vi.mocked(http.get).mockResolvedValue({ messages: [], has_more: false })
    const signal = new AbortController().signal

    await fetchHistory("s1", {}, signal)
    await fetchHistory("s1", { before: "message_b" })
    await fetchHistory("s1", { after: "message_a", turns: 1 })

    expect(vi.mocked(http.get).mock.calls).toEqual([
      ["/api/agent/session/s1/history?turns=8", { signal }],
      ["/api/agent/session/s1/history?turns=8&before=message_b", { signal: undefined }],
      ["/api/agent/session/s1/history?turns=1&after=message_a", { signal: undefined }],
    ])
  })

  it("resumes live catch-up from the newest confirmed message, not an optimistic echo", () => {
    expect(newestPersistedId([message("message_a"), message("message_b"), { ...message("tmp-1"), role: "user" }]))
      .toBe("message_b")
    expect(newestPersistedId([message("tmp-1")])).toBeUndefined()
    expect(newestPersistedId(undefined)).toBeUndefined()
  })

  it("recognises a page whose anchor message was deleted", () => {
    expect(isHistoryCursorGone(new ApiError(409, "HISTORY_CURSOR_GONE", "gone"))).toBe(true)
    expect(isHistoryCursorGone(new ApiError(409, "HTTP_409", "conflict"))).toBe(false)
    expect(isHistoryCursorGone(new Error("offline"))).toBe(false)
  })

  it("puts the turns before the oldest held message in front of it", async () => {
    useStreamStore.getState().mergeHistory("s1", [message("message_c"), message("message_d")], true)
    vi.mocked(http.get).mockResolvedValueOnce({ messages: [message("message_a"), message("message_b")], has_more: false })

    await loadOlderHistory("s1")

    expect(http.get).toHaveBeenCalledWith("/api/agent/session/s1/history?turns=8&before=message_c", { signal: undefined })
    expect(held()).toEqual(["message_a", "message_b", "message_c", "message_d"])
    expect(useStreamStore.getState().history.get("s1")).toEqual({ hasMore: false, loadingOlder: false })
  })

  it("asks once while an older page is already on its way", async () => {
    useStreamStore.getState().mergeHistory("s1", [message("message_c")], true)
    let resolve!: (page: unknown) => void
    vi.mocked(http.get).mockReturnValueOnce(new Promise((r) => { resolve = r }))

    const first = loadOlderHistory("s1")
    await loadOlderHistory("s1")
    resolve({ messages: [message("message_b")], has_more: true })
    await first

    expect(http.get).toHaveBeenCalledTimes(1)
    expect(held()).toEqual(["message_b", "message_c"])
  })
})
