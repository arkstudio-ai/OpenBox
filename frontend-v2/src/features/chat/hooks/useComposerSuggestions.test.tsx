import { act, cleanup, renderHook, waitFor } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"
import type { SuggestionsPart } from "@/shared/types/api"
import { useComposerSuggestions } from "./useComposerSuggestions"

afterEach(() => { cleanup(); vi.useRealTimers() })
const suggestions: SuggestionsPart = { id: "p1", type: "suggestions", items: [
  { label: "精简开头", prompt: "请把开头精简成三句话", mode: "send" },
] }
const defaults = { busy: false, draft: "", hasAttachments: false, suggestions, sessionKey: "s1" }

it("sends the full prompt exactly once across double clicks and dismisses the row", async () => {
  const onSend = vi.fn()
  const { result } = renderHook(() => useComposerSuggestions({ ...defaults, onSend, onFill: vi.fn(), onFailure: vi.fn() }))
  act(() => {
    result.current.select(suggestions.items[0])
    result.current.select(suggestions.items[0])
  })
  await waitFor(() => expect(onSend).toHaveBeenCalledExactlyOnceWith(suggestions.items[0].prompt))
  expect(result.current.visible).toBeUndefined()
})

it.each([{ busy: true }, { draft: " " }, { hasAttachments: true }])("hides and refuses clicks with %s", (state) => {
  const onSend = vi.fn()
  const { result } = renderHook(() => useComposerSuggestions({ ...defaults, ...state, onSend, onFill: vi.fn(), onFailure: vi.fn() }))
  act(() => result.current.select(suggestions.items[0]))
  expect(result.current.visible).toBeUndefined()
  expect(onSend).not.toHaveBeenCalled()
})

it("fills incomplete suggestions without submitting", () => {
  const onFill = vi.fn(), onSend = vi.fn()
  const { result } = renderHook(() => useComposerSuggestions({ ...defaults, onSend, onFill, onFailure: vi.fn() }))
  act(() => result.current.select({ ...suggestions.items[0], mode: "draft" }))
  expect(onFill).toHaveBeenCalledExactlyOnceWith(suggestions.items[0].prompt)
  expect(onSend).not.toHaveBeenCalled()
})

it("restores failed requests as editable drafts", async () => {
  const onFailure = vi.fn()
  const { result } = renderHook(() => useComposerSuggestions({ ...defaults,
    onSend: vi.fn().mockRejectedValue(new Error("offline")), onFill: vi.fn(), onFailure,
  }))
  act(() => result.current.select(suggestions.items[0]))
  await waitFor(() => expect(onFailure).toHaveBeenCalledExactlyOnceWith(suggestions.items[0].prompt))
})

it("does not restore an old session's failed prompt in a new session", async () => {
  let reject!: (error: Error) => void
  const onSend = vi.fn(() => new Promise<void>((_, fail) => { reject = fail }))
  const onFailure = vi.fn()
  const { result, rerender } = renderHook(({ sessionKey }) => useComposerSuggestions({ ...defaults,
    sessionKey, onSend, onFill: vi.fn(), onFailure,
  }), { initialProps: { sessionKey: "s1" } })
  act(() => result.current.select(suggestions.items[0]))
  await waitFor(() => expect(onSend).toHaveBeenCalledOnce())
  rerender({ sessionKey: "s2" })
  await act(async () => reject(new Error("offline")))
  expect(onFailure).not.toHaveBeenCalled()
})

const now = new Date("2026-09-10T12:00:00Z")
const pendingPart: SuggestionsPart = {
  ...suggestions, items: [], status: "pending", expires_at: "2026-09-10T12:01:00Z",
}

function pendingHook(part: SuggestionsPart | undefined = pendingPart) {
  vi.useFakeTimers()
  vi.setSystemTime(now)
  const onSend = vi.fn()
  const hook = renderHook<ReturnType<typeof useComposerSuggestions>, { suggestions?: SuggestionsPart }>(({ suggestions: value }) => useComposerSuggestions({
    ...defaults, suggestions: value, onSend, onFill: vi.fn(), onFailure: vi.fn(),
  }), { initialProps: { suggestions: part } })
  act(() => vi.advanceTimersByTime(0))
  return { ...hook, onSend }
}

it.each(["completed", "unavailable"] as const)("ends the wait on a %s result, including no suggestions", (status) => {
  const { result, rerender, onSend } = pendingHook()
  expect(result.current.loading).toBe(true)
  expect(result.current.visible).toBeUndefined()
  act(() => result.current.select(suggestions.items[0]))
  expect(onSend).not.toHaveBeenCalled()
  rerender({ suggestions: { ...pendingPart, status, expires_at: null } })
  expect(result.current.loading).toBe(false)
  expect(result.current.visible).toBeUndefined()
})

it("replaces the wait with suggestions without changing the normal send path", () => {
  const { result, rerender } = pendingHook()
  rerender({ suggestions: { ...suggestions, status: "completed" } })
  expect(result.current.loading).toBe(false)
  expect(result.current.visible).toEqual({ ...suggestions, status: "completed" })
})

it("expires after worker death and does not restart when a stale snapshot is shown again", () => {
  const { result, rerender } = pendingHook()
  expect(result.current.loading).toBe(true)
  act(() => vi.advanceTimersByTime(60_001))
  expect(result.current.loading).toBe(false)
  rerender({ suggestions: undefined })
  rerender({ suggestions: pendingPart })
  act(() => vi.advanceTimersByTime(0))
  expect(result.current.loading).toBe(false)
})

it("a hidden wait that expires cannot reappear after scrolling back or switching sessions", () => {
  const { result, rerender } = pendingHook()
  rerender({ suggestions: undefined })
  act(() => vi.advanceTimersByTime(60_001))
  rerender({ suggestions: pendingPart })
  act(() => vi.advanceTimersByTime(0))
  expect(result.current.loading).toBe(false)
})

it.each([undefined, "invalid", "2026-09-10T11:59:00Z"])("ignores an invalid or expired pending deadline: %s", (expires_at) => {
  const { result } = pendingHook({ ...pendingPart, expires_at })
  expect(result.current.loading).toBe(false)
  expect(result.current.visible).toBeUndefined()
})

it.each([{ busy: true }, { draft: "my draft" }, { hasAttachments: true }])("the wait respects composer state %s", (state) => {
  vi.useFakeTimers()
  vi.setSystemTime(now)
  const { result } = renderHook(() => useComposerSuggestions({ ...defaults, ...state,
    suggestions: pendingPart, onSend: vi.fn(), onFill: vi.fn(), onFailure: vi.fn(),
  }))
  act(() => vi.advanceTimersByTime(0))
  expect(result.current.loading).toBe(false)
})
