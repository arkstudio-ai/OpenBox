import { act, cleanup, renderHook } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { useWorkspaceUi } from "../stores/ui"
import { useSidebarLayout } from "./useSidebarLayout"

let small = false
const listeners = new Set<() => void>()
beforeEach(() => {
  small = false
  listeners.clear()
  localStorage.clear()
  useWorkspaceUi.setState({ sidebarCollapsed: false, mobileSidebarOpen: false })
  vi.stubGlobal("matchMedia", () => ({
    get matches() {
      return small
    },
    addEventListener: (_event: string, listener: () => void) => listeners.add(listener),
    removeEventListener: (_event: string, listener: () => void) => listeners.delete(listener),
  }))
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe("narrow-screen navigation", () => {
  it("starts closed on a phone even if the desktop sidebar is saved open", () => {
    small = true
    const { result } = renderHook(useSidebarLayout)
    expect(result.current.compact).toBe(true)
    expect(result.current.open).toBe(false)
    expect(useWorkspaceUi.getState().sidebarCollapsed).toBe(false)
  })
  it("opens and closes the mobile drawer without rewriting desktop preferences", () => {
    small = true
    const { result } = renderHook(useSidebarLayout)
    act(() => result.current.toggle())
    expect(result.current.open).toBe(true)
    act(() => result.current.close())
    expect(result.current.open).toBe(false)
    expect(useWorkspaceUi.getState().sidebarCollapsed).toBe(false)
    expect(localStorage.getItem("bossip:workspace-ui")).toBeNull()
  })
  it("responds to crossing the breakpoint and restores the desktop layout", () => {
    const { result } = renderHook(useSidebarLayout)
    expect(result.current.open).toBe(true)
    act(() => {
      small = true
      listeners.forEach((listener) => listener())
    })
    expect(result.current.open).toBe(false)
    act(() => {
      small = false
      listeners.forEach((listener) => listener())
    })
    expect(result.current.open).toBe(true)
  })
  it("keeps the desktop toggle working and persisted", () => {
    const { result } = renderHook(useSidebarLayout)
    act(() => result.current.toggle())
    expect(result.current.open).toBe(false)
    expect(JSON.parse(localStorage.getItem("bossip:workspace-ui")!).sidebarCollapsed).toBe(true)
  })
})
