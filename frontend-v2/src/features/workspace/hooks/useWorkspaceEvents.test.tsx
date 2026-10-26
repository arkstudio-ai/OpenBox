import type { PropsWithChildren } from "react"
import { act, cleanup, renderHook } from "@testing-library/react"
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { http } from "@/shared/api/http"
import { useChatEvents } from "@/features/chat/hooks/useChatEvents"
import { useStreamStore } from "@/features/chat/stores/stream"
import { useSessionsQuery } from "../api/sessions"
import { useWorkspaceEvents } from "./useWorkspaceEvents"

const { listeners } = vi.hoisted(() => ({ listeners: new Map<string, Set<(data: unknown) => void>>() }))
vi.mock("@/shared/ws/client", () => ({ wsClient: { connect: vi.fn(), disconnect: vi.fn(),
  on: (event: string, callback: (data: unknown) => void) => {
    if (!listeners.has(event)) listeners.set(event, new Set())
    listeners.get(event)!.add(callback)
    return () => listeners.get(event)!.delete(callback)
  } } }))
vi.mock("@/shared/api/http", async (original) => ({
  ...await original<typeof import("@/shared/api/http")>(), http: { get: vi.fn() },
}))
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (value: string) => value }) }))
vi.mock("@/shared/ui/Toast", () => ({ toast: vi.fn() }))
let client: QueryClient
const tick = (ms = 10) => act(() => vi.advanceTimersByTimeAsync(ms))
const emit = (event: string, data = {}) => act(() => listeners.get(event)?.forEach((f) => f(data)))
beforeEach(() => {
  vi.useFakeTimers(); vi.clearAllMocks(); listeners.clear()
  useAuthStore.setState({ user: { id: "owner" } as never })
  useWorkspaceStore.setState({ currentId: "workspace" })
  useStreamStore.setState({ messages: new Map(), history: new Map(), status: new Map(), statusGeneration: new Map(),
    terminalStatusGeneration: new Map() })
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  vi.mocked(http.get).mockResolvedValue([])
})
afterEach(() => { cleanup(); client.clear(); vi.useRealTimers() })

it("hydrates 465 historical statuses without fetching the list, balance or cards 465 times", async () => {
  const balance = vi.fn(async () => ({ balance: 10 }))
  const wrapper = ({ children }: PropsWithChildren) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
  renderHook(() => {
    useWorkspaceEvents()
    useChatEvents("current")
    useSessionsQuery()
    useQuery({ queryKey: ["billing", "owner", "workspace"], queryFn: balance })
  }, { wrapper })
  await tick()
  emit("__connected")
  await tick()
  expect(http.get).toHaveBeenCalledTimes(2)
  expect(balance).toHaveBeenCalledTimes(2)
  const invalidations = vi.spyOn(client, "invalidateQueries")
  // Frames arrive across network turns, so ordinary request dedup cannot
  // conceal a per-frame refetch as it would with one synchronous burst.
  for (let n = 0; n < 465; n++) {
    emit("session.status", { sessionId: `old-${n}`, status: "idle", generation: 3, recovery: true })
    await tick()
  }
  expect(http.get).toHaveBeenCalledTimes(2)
  expect(balance).toHaveBeenCalledTimes(2)
  expect(invalidations).not.toHaveBeenCalled()
  expect(useStreamStore.getState().status.get("old-464")).toBe("idle")
  expect(useStreamStore.getState().statusGeneration.get("old-464")).toBe(3)

  emit("session.status", { sessionId: "current", status: "busy", generation: 4 })
  await tick()
  emit("session.status", { sessionId: "current", status: "idle", generation: 4 })
  await tick()
  expect(http.get).toHaveBeenCalledTimes(4)
  expect(balance).toHaveBeenCalledTimes(4)
  expect(useStreamStore.getState().status.get("current")).toBe("idle")
  // A late snapshot must not roll the current generation backwards.
  emit("session.status", { sessionId: "current", status: "busy", generation: 2, recovery: true })
  expect(useStreamStore.getState().status.get("current")).toBe("idle")
})
