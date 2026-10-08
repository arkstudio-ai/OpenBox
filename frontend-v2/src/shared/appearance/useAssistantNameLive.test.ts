import { renderHook } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { wsClient } from "@/shared/ws/client"
import { useAppearanceStore } from "./store"
import { useAssistantNameLive } from "./useAssistantNameLive"

afterEach(() => {
  vi.restoreAllMocks()
  useAppearanceStore.setState({ assistantName: "" })
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

describe("useAssistantNameLive", () => {
  it("shows a rename at once, wherever it was made", () => {
    useAppearanceStore.setState({ assistantName: "Mary" })
    const { handlers } = captured()
    renderHook(() => useAssistantNameLive())
    handlers.get("assistant.renamed")!({ userId: "u1", name: "小七" } as never)
    expect(useAppearanceStore.getState().assistantName).toBe("小七")
    handlers.get("assistant.renamed")!({ userId: "u1", name: "" } as never)
    expect(useAppearanceStore.getState().assistantName).toBe("") // the default name again
  })

  it("reads the name again after a reconnect and stops listening when unmounted", async () => {
    const { handlers, offs } = captured()
    const get = vi.spyOn(http, "get").mockResolvedValue({ name: "小七" })
    const { unmount } = renderHook(() => useAssistantNameLive())
    handlers.get("__connected")!({} as never)
    await vi.waitFor(() => expect(useAppearanceStore.getState().assistantName).toBe("小七"))
    expect(get).toHaveBeenCalledWith("/api/assistant/name")
    unmount()
    expect(offs.sort()).toEqual(["__connected", "assistant.renamed"])
  })
})
