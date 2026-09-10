import { beforeEach, describe, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import type { MessageWithParts } from "@/shared/types/api"
import { fetchMessageSnapshot, sendPromptAsync } from "./messages"

vi.mock("@/shared/api/http", () => ({
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

describe("complete message snapshots", () => {
  beforeEach(() => vi.resetAllMocks())
  const message = (id: number): MessageWithParts => ({
    id: String(id), session_id: "s1", role: "assistant", created_at: String(id), parts: [],
  })

  it("loads past 200 messages before exposing the latest suggestions", async () => {
    const latest: MessageWithParts = { ...message(200), finish: "stop", parts: [
      { type: "suggestions", id: "p-latest", items: [] },
    ] }
    vi.mocked(http.get).mockResolvedValueOnce(Array.from({ length: 200 }, (_, i) => message(i)))
      .mockResolvedValueOnce([latest])
    const signal = new AbortController().signal
    const result = await fetchMessageSnapshot("s1", signal)
    expect(result).toHaveLength(201)
    expect(result[200]).toEqual(latest)
    expect(http.get).toHaveBeenNthCalledWith(2, "/api/agent/session/s1/message?offset=200&limit=200", { signal })
  })

  it("does not expose partial history when a later page fails", async () => {
    vi.mocked(http.get).mockResolvedValueOnce(Array.from({ length: 200 }, (_, i) => message(i)))
      .mockRejectedValueOnce(new Error("offline"))
    await expect(fetchMessageSnapshot("s1")).rejects.toThrow("offline")
  })

  it("does not loop when an intermediary repeats the first page", async () => {
    vi.mocked(http.get).mockResolvedValue(Array.from({ length: 200 }, (_, i) => message(i)))
    await expect(fetchMessageSnapshot("s1")).rejects.toThrow("Message pagination did not advance")
    expect(http.get).toHaveBeenCalledTimes(2)
  })
})
