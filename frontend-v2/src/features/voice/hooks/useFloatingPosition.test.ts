import { act, cleanup, renderHook } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"
import { useFloatingPosition } from "./useFloatingPosition"

afterEach(() => { cleanup(); localStorage.clear(); vi.unstubAllGlobals() })

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
