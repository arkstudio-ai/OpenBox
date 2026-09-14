// How often an open session reads committed events it may have missed: slowly
// while the watermark socket carries hints, quickly while it cannot, never
// fast in a hidden tab, and at once when the tab is shown again.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, renderHook } from "@testing-library/react"
import type { ReactNode } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import type { AuthUser } from "@/shared/types/api"
import { useTrajectoryAccess } from "../stores/access"
import { useTrajectorySync } from "./useTrajectorySync"

const socket = vi.hoisted(() => {
  const handlers = new Map<string, Set<() => void>>()
  return {
    connected: false,
    handlers,
    on(event: string, handler: () => void) {
      const set = handlers.get(event) ?? new Set()
      set.add(handler)
      handlers.set(event, set)
      return () => {
        set.delete(handler)
      }
    },
    emit(event: string) {
      for (const handler of handlers.get(event) ?? []) handler()
    },
  }
})

const engine = vi.hoisted(() => ({
  poll: vi.fn(async () => undefined),
  subscribe: () => () => undefined,
  getSnapshot: () => null,
}))

vi.mock("../api/socket", () => ({ trajectorySocket: socket }))
vi.mock("../api/registry", () => ({
  acquireSync: vi.fn(),
  releaseSync: vi.fn(),
  peekSync: () => engine,
  subscribeRegistry: () => () => undefined,
  purgeSyncs: vi.fn(),
  parseTargetKey: () => null,
}))

function setVisibility(state: DocumentVisibilityState) {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state })
}

function mount() {
  const client = new QueryClient()
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
  return renderHook(() => useTrajectorySync("ses_b", true), { wrapper })
}

const advance = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms))

beforeEach(() => {
  vi.useFakeTimers()
  setVisibility("visible")
  socket.connected = false
  socket.handlers.clear()
  engine.poll.mockClear()
  useTrajectoryAccess.setState({ epoch: 0, denied: null })
  useAuthStore.setState({
    user: { id: "admin-a", role: "admin" } as unknown as AuthUser,
    isAuthenticated: true,
    isLoading: false,
  })
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  setVisibility("visible")
})

describe("event catch-up polling", () => {
  it("polls every 10 s while the socket is open and every 2 s as soon as it drops", async () => {
    socket.connected = true
    mount()
    await advance(9_999)
    expect(engine.poll).not.toHaveBeenCalled()
    await advance(1)
    expect(engine.poll).toHaveBeenCalledTimes(1)

    socket.connected = false
    act(() => socket.emit("__disconnected"))
    await advance(1_999)
    expect(engine.poll).toHaveBeenCalledTimes(1)
    await advance(1)
    expect(engine.poll).toHaveBeenCalledTimes(2)
    await advance(2_000)
    expect(engine.poll).toHaveBeenCalledTimes(3)

    socket.connected = true
    act(() => socket.emit("__connected"))
    await advance(9_999)
    expect(engine.poll).toHaveBeenCalledTimes(3)
    await advance(1)
    expect(engine.poll).toHaveBeenCalledTimes(4)
  })

  it("slows down in a hidden tab and reads at once when it is shown again", async () => {
    mount()
    setVisibility("hidden")
    act(() => void document.dispatchEvent(new Event("visibilitychange")))
    await advance(4_999)
    expect(engine.poll).not.toHaveBeenCalled()
    await advance(1)
    expect(engine.poll).toHaveBeenCalledTimes(1)

    setVisibility("visible")
    act(() => void document.dispatchEvent(new Event("visibilitychange")))
    expect(engine.poll).toHaveBeenCalledTimes(2)
    await advance(1_999)
    expect(engine.poll).toHaveBeenCalledTimes(2)
    await advance(1)
    expect(engine.poll).toHaveBeenCalledTimes(3)
  })

  it("keeps polling while the socket keeps opening and dropping", async () => {
    mount()
    // For 10 s the socket opens every 500 ms and drops again 100 ms later.
    for (let cycle = 0; cycle < 20; cycle += 1) {
      await advance(400)
      socket.connected = true
      act(() => socket.emit("__connected"))
      await advance(100)
      socket.connected = false
      act(() => socket.emit("__disconnected"))
    }
    await advance(0)
    // Each change of state re-plans the next read from the previous one, never from the change.
    expect(engine.poll.mock.calls.length).toBeGreaterThanOrEqual(4)
  })

  it("stops polling and listening once the page closes", async () => {
    const { unmount } = mount()
    expect(socket.handlers.get("__connected")?.size).toBe(1)
    unmount()
    await advance(60_000)
    expect(engine.poll).not.toHaveBeenCalled()
    expect(socket.handlers.get("__connected")?.size).toBe(0)
    expect(socket.handlers.get("__disconnected")?.size).toBe(0)
  })
})
