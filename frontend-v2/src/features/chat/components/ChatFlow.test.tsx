import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react"
import { emitAppEvent } from "@/shared/events/bus"
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
  vi.restoreAllMocks()
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
  it("keeps the long-history viewport responsive after history refreshes", () => {
    // Supply layout only; use the real virtualizer and its scroll listeners.
    // A refresh changes the row props while the reader stays scrolled down.
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockImplementation(function (this: HTMLElement) {
      return this.classList.contains("scr") ? 520 : 260
    })
    vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(800)
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(520)
    vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(60 * 260)
    const historyScrollRef = { current: null as HTMLDivElement | null }
    const turns = Array.from({ length: 60 }, (_, i) => userTurn(`m${i}`))
    const props = { sessionId: "s1", busy: false, historyScrollRef }
    const { rerender } = render(<ChatFlow {...props} turns={turns.slice(0, 50)} />)
    rerender(<ChatFlow {...props} turns={turns} />)
    const viewport = historyScrollRef.current!
    fireEvent.scroll(viewport, { target: { scrollTop: 52 * 260 } })
    expect(screen.getByText("m59")).toBeTruthy()
    expect(screen.queryByText("m0")).toBeNull()

    rerender(<ChatFlow {...props} turns={[...turns]} />)
    fireEvent.scroll(viewport, { target: { scrollTop: 25 * 260 } })
    expect(screen.getByText("m25")).toBeTruthy()
    expect(screen.queryByText("m59")).toBeNull()
    fireEvent.scroll(viewport, { target: { scrollTop: 52 * 260 } })
    expect(screen.getByText("m59")).toBeTruthy()
  })

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

describe("ChatFlow reveal", () => {
  let revealed: string[]
  beforeEach(() => {
    revealed = []
    // jsdom has no layout or scrolling: the list shows 0–500px, every row sits
    // below it except m2, and scrollIntoView records which row was asked for.
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const key = this.dataset.turnKey
      const top = key === undefined ? 0 : key === "m2" ? 100 : 900
      return { top, bottom: top + (key === undefined ? 500 : 80) } as DOMRect
    })
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
      configurable: true,
      value(this: HTMLElement) {
        revealed.push(this.dataset.turnKey ?? "")
      },
    })
  })
  afterEach(() => {
    delete (HTMLElement.prototype as Partial<HTMLElement>).scrollIntoView
  })

  it("brings a voice turn into view once its message reaches the list", () => {
    vi.useFakeTimers()
    const props = { sessionId: "s1", busy: false }
    const { rerender } = render(<ChatFlow {...props} turns={[userTurn("m1"), userTurn("m2")]} />)
    act(() => emitAppEvent("chat.reveal", { messageId: "voice-1" }))
    expect(revealed).toEqual([])
    rerender(<ChatFlow {...props} turns={[userTurn("m1"), userTurn("voice-1"), userTurn("m2")]} />)
    act(() => vi.advanceTimersByTime(150))
    expect(revealed).toEqual(["voice-1"])
    act(() => emitAppEvent("chat.reveal", { messageId: "m1" }))
    expect(revealed).toEqual(["voice-1", "m1"])
    // A row already in view stays put.
    act(() => emitAppEvent("chat.reveal", { messageId: "m2" }))
    expect(revealed).toEqual(["voice-1", "m1"])
  })

  it("gives up quietly on a message this list never shows", () => {
    vi.useFakeTimers()
    render(<ChatFlow sessionId="s1" busy={false} turns={[userTurn("m1")]} />)
    act(() => emitAppEvent("chat.reveal", { messageId: "elsewhere" }))
    act(() => vi.advanceTimersByTime(10_000))
    expect(revealed).toEqual([])
  })
})
