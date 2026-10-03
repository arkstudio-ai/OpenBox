import { act, cleanup, render } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import type { AssistantSnapshot } from "../api/assistant"
import { AssistantReadBoundary, VisibleAssistantAnswer } from "./AssistantReadBoundary"

const mutate = vi.hoisted(() => vi.fn())
vi.mock("../api/assistant", () => ({ useAssistantReadCursor: () => ({ mutate }) }))
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }))

let observers: Observer[] = []
class Observer {
  observe = vi.fn()
  disconnect = vi.fn()
  constructor(readonly callback: IntersectionObserverCallback) { observers.push(this) }
  show(visible: boolean) {
    this.callback([{ isIntersecting: visible, intersectionRect: { height: visible ? 50 : 0, width: 200 } } as IntersectionObserverEntry],
      this as unknown as IntersectionObserver)
  }
}
const answer = { message_id: "answer", sequence: 10, available: true, display_token: "signed-display" }
const snapshot = { answers: [answer], last_seen_sequence: 0 } as AssistantSnapshot
beforeEach(() => {
  mutate.mockReset()
  observers = []
  vi.stubGlobal("IntersectionObserver", Observer)
  vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible")
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

function mount(state = snapshot) {
  return render(<AssistantReadBoundary snapshot={state}><VisibleAssistantAnswer messageId="answer">Final answer</VisibleAssistantAnswer></AssistantReadBoundary>)
}

describe("assistant read receipts", () => {
  it("requires the final answer to intersect the viewport and deduplicates observations", () => {
    mount()
    expect(mutate).not.toHaveBeenCalled()
    act(() => observers[0].show(false))
    expect(mutate).not.toHaveBeenCalled()
    act(() => { observers[0].show(true); observers[0].show(true) })
    expect(mutate).toHaveBeenCalledExactlyOnceWith(answer)
  })
  it("does not read on background auto-scroll and remeasures when foregrounded", () => {
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden")
    mount()
    act(() => observers[0].show(true))
    expect(mutate).not.toHaveBeenCalled()
    expect(observers[0].observe).not.toHaveBeenCalled()
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible")
    act(() => document.dispatchEvent(new Event("visibilitychange")))
    expect(mutate).not.toHaveBeenCalled()
    expect(observers[0].observe).toHaveBeenCalledTimes(1)
    act(() => observers[0].show(true))
    expect(mutate).toHaveBeenCalledOnce()
  })
  it("hides revoked answers and never advances a cursor for them", () => {
    const view = mount({ ...snapshot, answers: [{ ...answer, available: false, display_token: undefined }] })
    expect(view.queryByText("Final answer")).toBeNull()
    expect(view.getByText("assistant.sourceUnavailable")).toBeTruthy()
    expect(observers).toHaveLength(0)
    expect(mutate).not.toHaveBeenCalled()
  })
  it("does not resend a read receipt already recorded by another device", () => {
    mount({ ...snapshot, last_seen_sequence: 12 })
    act(() => observers[0].show(true))
    expect(mutate).not.toHaveBeenCalled()
  })
})
