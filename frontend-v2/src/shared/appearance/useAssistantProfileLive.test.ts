import { act, renderHook } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { wsClient } from "@/shared/ws/client"
import { DEFAULT_ASSISTANT_PROFILE } from "./assistant-profile"
import { useAppearanceStore } from "./store"
import { useAssistantProfileLive } from "./useAssistantProfileLive"

beforeEach(() => useAuthStore.getState().setAuth("test", { id: "u1", username: "test", role: "user" }))

afterEach(() => {
  vi.restoreAllMocks()
  useAuthStore.getState().clearAuth()
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
  it("resets the previous account's revision and ignores its delayed read and notification", async () => {
    useAppearanceStore.getState().applyAssistantView({ address: "旧账号", revision: 90 })
    const { handlers } = captured()
    let finish!: (value: unknown) => void
    vi.spyOn(http, "get").mockImplementation(() => new Promise((resolve) => { finish = resolve }) as never)
    renderHook(() => useAssistantProfileLive())
    const oldHandler = handlers.get("assistant.profile.updated")!
    handlers.get("__connected")!({} as never)
    act(() => useAuthStore.getState().setAuth("other", { id: "u2", username: "other", role: "user" }))
    expect(useAppearanceStore.getState().assistant).toEqual(DEFAULT_ASSISTANT_PROFILE)
    useAppearanceStore.getState().applyAssistantView({ address: "新账号", revision: 1 })
    await act(async () => finish({ address: "旧账号", revision: 91 }))
    oldHandler({ userId: "u1", profile: { address: "旧推送", revision: 92 } } as never)
    handlers.get("assistant.profile.updated")!({ userId: "u1", profile: { address: "串号", revision: 93 } } as never)
    expect(useAppearanceStore.getState().assistant).toMatchObject({ address: "新账号", revision: 1 })
  })

  it("keeps a live revision when an older preferences hydration arrives", () => {
    useAppearanceStore.getState().applyAssistantView({ address: "当前", revision: 4 })
    useAppearanceStore.getState().hydrateFromServer({ extra: { assistant_profile: { address: "旧" }, assistant_profile_revision: 3 } } as never, "u1")
    useAppearanceStore.getState().hydrateFromServer({ extra: { assistant_profile: { address: "其他人" }, assistant_profile_revision: 99 } } as never, "u2")
    expect(useAppearanceStore.getState().assistant).toMatchObject({ address: "当前", revision: 4 })
  })

  it("keeps the newer committed revision when an old notification arrives late", () => {
    useAppearanceStore.getState().applyAssistantView({ address: "林姐", revision: 3 })
    useAppearanceStore.getState().applyAssistantView({ address: "旧称呼", revision: 2 })
    expect(useAppearanceStore.getState().assistant).toMatchObject({ address: "林姐", revision: 3 })
  })

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
