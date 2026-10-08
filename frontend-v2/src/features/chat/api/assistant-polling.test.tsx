import type { PropsWithChildren } from "react"
import { act, cleanup, renderHook } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { useAssistantResultTarget, useAssistantTask } from "./assistant"

vi.mock("@/shared/api/http", async (original) => ({
  ...await original<typeof import("@/shared/api/http")>(), http: { get: vi.fn() },
}))

let client: QueryClient
beforeEach(() => {
  vi.useFakeTimers()
  useAuthStore.setState({ user: { id: "owner" } as never })
  useWorkspaceStore.setState({ currentId: "workspace" })
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  vi.mocked(http.get).mockResolvedValue({})
})
afterEach(() => { cleanup(); client.clear(); vi.clearAllMocks(); vi.useRealTimers() })
const wrapper = ({ children }: PropsWithChildren) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
const tick = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms))
const reads = (path: string) => vi.mocked(http.get).mock.calls.filter(([url]) => url === path).length

function useTaskCard(): unknown { return useAssistantTask("task").data }
function useNotificationTarget(): unknown { return useAssistantResultTarget("result").data }

// Assistant events invalidate both views; their own timers are only a fallback.
it.each([
  ["task card", useTaskCard, "/api/assistant/tasks/task"],
  ["notification target", useNotificationTarget, "/api/assistant/results/result/target"],
] as const)("re-reads a %s every 30 seconds, not every 5", async (_label, useView, path) => {
  renderHook(useView, { wrapper })
  await tick(10)
  expect(reads(path)).toBe(1)
  await tick(25_000)
  expect(reads(path)).toBe(1)
  await tick(5_100)
  expect(reads(path)).toBe(2)
})
