import { act, cleanup, renderHook } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"
import { useFloatingPosition } from "./useFloatingPosition"

afterEach(() => {
  cleanup()
  localStorage.clear()
  vi.unstubAllGlobals()
})

it("keeps the launcher above composer controls after restoring an old bottom position or resizing", () => {
  vi.stubGlobal("innerWidth", 564)
  vi.stubGlobal("innerHeight", 857)
  localStorage.setItem("test-orb", JSON.stringify({ x: 468, y: 737 }))
  const { result } = renderHook(() => useFloatingPosition("test-orb", { width: 72, height: 96 }, 160))
  expect(result.current.style.top + result.current.style.height).toBeLessThanOrEqual(857 - 160 - 16)
  vi.stubGlobal("innerHeight", 400)
  act(() => window.dispatchEvent(new Event("resize")))
  expect(result.current.style.top + result.current.style.height).toBeLessThanOrEqual(400 - 160 - 16)
  act(() => result.current.reset())
  expect(result.current.style.top + result.current.style.height).toBeLessThanOrEqual(400 - 160 - 16)
})

it("anchors the larger companion at the top right, preserves a drag and resets to the new default", () => {
  vi.stubGlobal("innerWidth", 1200)
  vi.stubGlobal("innerHeight", 900)
  const { result } = renderHook(() =>
    useFloatingPosition("large-orb", { width: 104, height: 128 }, 160, "top-right"),
  )
  expect(result.current.style).toEqual({ left: 1072, top: 72, width: 104, height: 128 })
  act(() => result.current.handlers.onKeyDown({ key: "ArrowLeft", preventDefault() {} } as never))
  expect(result.current.style.left).toBe(1064)
  act(() => result.current.reset())
  expect(result.current.style.left).toBe(1072)
  vi.stubGlobal("innerHeight", 320)
  act(() => window.dispatchEvent(new Event("resize")))
  expect(result.current.style.top + result.current.style.height).toBeLessThanOrEqual(320 - 160 - 16)
})
