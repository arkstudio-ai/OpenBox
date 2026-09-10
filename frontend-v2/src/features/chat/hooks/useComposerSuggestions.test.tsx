import { act, cleanup, renderHook, waitFor } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"
import type { SuggestionsPart } from "@/shared/types/api"
import { useComposerSuggestions } from "./useComposerSuggestions"

afterEach(cleanup)
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
