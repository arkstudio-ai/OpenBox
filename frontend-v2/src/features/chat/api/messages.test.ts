import { beforeEach, describe, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { sendPromptAsync } from "./messages"

vi.mock("@/shared/api/http", () => ({
  http: { post: vi.fn() },
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
