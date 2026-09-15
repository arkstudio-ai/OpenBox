import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, cleanup, render, screen } from "@testing-library/react"
import type { MessageWithParts } from "@/shared/types/api"
import type { Turn } from "../lib/turn-view"
import { ChatFlow } from "./ChatFlow"

vi.mock("react-i18next", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-i18next")>()),
  useTranslation: () => ({ t: (key: string) => key }),
}))
// The rows are not under test here; their own suites cover them.
vi.mock("./UserBubble", () => ({
  UserBubble: ({ message }: { message: MessageWithParts }) => <p>{message.id}</p>,
}))
vi.mock("./AssistantTurn", () => ({ AssistantTurn: () => <p>assistant</p>, TypingRow: () => null }))

beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  )
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

function userTurn(id: string): Turn {
  const message: MessageWithParts = {
    id,
    session_id: "s1",
    role: "user",
    created_at: id,
    parts: [{ id: `${id}-text`, type: "text", text: id }],
  }
  return { kind: "user", key: id, message }
}

describe("ChatFlow older turns", () => {
  // jsdom lays nothing out, so every column counts as too short to scroll —
  // the case where older turns must load without the reader scrolling at all.
  it("asks for older turns while the column cannot scroll, once per top turn", () => {
    const onLoadOlder = vi.fn()
    const props = { sessionId: "s1", busy: false, hasMore: true, onLoadOlder }

    const { rerender } = render(<ChatFlow {...props} turns={[userTurn("m3")]} />)
    expect(onLoadOlder).toHaveBeenCalledTimes(1)

    // A live turn arriving at the bottom is not a reason to ask again.
    rerender(<ChatFlow {...props} turns={[userTurn("m3"), userTurn("m4")]} />)
    expect(onLoadOlder).toHaveBeenCalledTimes(1)

    // Once the older page lands there is a new top, and it may still be short.
    rerender(<ChatFlow {...props} turns={[userTurn("m1"), userTurn("m3"), userTurn("m4")]} />)
    expect(onLoadOlder).toHaveBeenCalledTimes(2)
  })

  it("leaves the top alone when nothing older exists or a page is already loading", () => {
    const onLoadOlder = vi.fn()
    const turns = [userTurn("m1")]

    const { rerender } = render(
      <ChatFlow sessionId="s1" busy={false} turns={turns} hasMore={false} onLoadOlder={onLoadOlder} />,
    )
    rerender(<ChatFlow sessionId="s1" busy={false} turns={turns} hasMore loadingOlder onLoadOlder={onLoadOlder} />)

    expect(onLoadOlder).not.toHaveBeenCalled()
    expect(screen.getByRole("status", { name: "loadingOlder" })).toBeTruthy()
  })

  // A column too short to scroll can never leave the top and come back, so a
  // failed page is asked for again after a pause instead of never.
  it("asks again after a pause when an older page failed, and pauses longer each time", () => {
    vi.useFakeTimers()
    const onLoadOlder = vi.fn()
    const props = { sessionId: "s1", busy: false, hasMore: true, onLoadOlder }
    const turns = [userTurn("m3")]
    const { rerender } = render(<ChatFlow {...props} turns={turns} />)
    expect(onLoadOlder).toHaveBeenCalledTimes(1)
    const fail = () => {
      rerender(<ChatFlow {...props} turns={turns} loadingOlder />)
      rerender(<ChatFlow {...props} turns={turns} loadingOlder={false} />)
    }

    fail()
    act(() => vi.advanceTimersByTime(1_999))
    expect(onLoadOlder).toHaveBeenCalledTimes(1)
    act(() => vi.advanceTimersByTime(1))
    expect(onLoadOlder).toHaveBeenCalledTimes(2)

    fail()
    act(() => vi.advanceTimersByTime(2_000))
    expect(onLoadOlder).toHaveBeenCalledTimes(2)
    act(() => vi.advanceTimersByTime(2_000))
    expect(onLoadOlder).toHaveBeenCalledTimes(3)

    // A page that lands resets the pause and asks for the next one at once.
    rerender(<ChatFlow {...props} turns={turns} loadingOlder />)
    rerender(<ChatFlow {...props} turns={[userTurn("m1"), ...turns]} />)
    expect(onLoadOlder).toHaveBeenCalledTimes(4)
    act(() => vi.advanceTimersByTime(60_000))
    expect(onLoadOlder).toHaveBeenCalledTimes(4)
  })
})
