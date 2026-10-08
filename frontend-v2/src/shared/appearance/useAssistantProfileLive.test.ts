import { renderHook } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { wsClient } from "@/shared/ws/client"
import { DEFAULT_ASSISTANT_PROFILE } from "./assistant-profile"
import { useAppearanceStore } from "./store"
import { useAssistantProfileLive } from "./useAssistantProfileLive"

afterEach(() => {
  vi.restoreAllMocks()
  useAppearanceStore.setState({ assistant: DEFAULT_ASSISTANT_PROFILE, assistantMeta: null })
})

/** The socket's handlers, captured as the hook registers them. */
function captured() {
  const handlers = new Map<string, (data: never) => void>()
  const offs: string[] = []
  vi.spyOn(wsClient, "on").mockImplementation(((event: string, handler: (data: never) => void) => {
    handlers.set(event, handler)
    return () => offs.push(event)
  }) as typeof wsClient.on)
  return { handlers, offs }
}

describe("useAssistantProfileLive", () => {
  it("shows a change at once, wherever it was made", () => {
    useAppearanceStore.setState({ assistant: { ...DEFAULT_ASSISTANT_PROFILE, name: "Mary" } })
    const { handlers } = captured()
    renderHook(() => useAssistantProfileLive())
    handlers.get("assistant.profile.updated")!({
      userId: "u1",
      profile: { ...DEFAULT_ASSISTANT_PROFILE, name: "小七", address: "老王", tone: "lively" },
    } as never)
    expect(useAppearanceStore.getState().assistant).toMatchObject({
      name: "小七",
      address: "老王",
      tone: "lively",
    })
    handlers.get("assistant.profile.updated")!({ userId: "u1", profile: { name: "", tone: "rude" } } as never)
    expect(useAppearanceStore.getState().assistant).toEqual(DEFAULT_ASSISTANT_PROFILE) // what does not hold falls back
    // Which parts were decided, and where the first meeting got to, come with it.
    handlers.get("assistant.profile.updated")!({
      userId: "u1",
      profile: {
        ...DEFAULT_ASSISTANT_PROFILE,
        address: "老王",
        decided: { address: { at: "t", via: "intro" } },
        intro: { status: "started", steps: { address: "answered" }, nudged: false },
      },
    } as never)
    expect(useAppearanceStore.getState().assistantMeta).toEqual({
      decided: { address: { at: "t", via: "intro" } },
      intro: { status: "started", steps: { address: "answered" }, nudged: false },
    })
  })

  it("reads it again after a reconnect and stops listening when unmounted", async () => {
    const { handlers, offs } = captured()
    const get = vi.spyOn(http, "get").mockResolvedValue({ ...DEFAULT_ASSISTANT_PROFILE, name: "小七" })
    const { unmount } = renderHook(() => useAssistantProfileLive())
    handlers.get("__connected")!({} as never)
    await vi.waitFor(() => expect(useAppearanceStore.getState().assistant.name).toBe("小七"))
    expect(get).toHaveBeenCalledWith("/api/assistant/profile")
    unmount()
    expect(offs.sort()).toEqual(["__connected", "assistant.profile.updated"])
  })
})
