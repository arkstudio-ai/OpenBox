import type { PropsWithChildren } from "react"
import { act, cleanup, renderHook } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { ApiError, http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { assistantKeys, useAssistantReadCursor, useAssistantSidebarUnread, useAssistantSnapshot,
  type AssistantSnapshot, type AssistantUnread } from "./assistant"

const { listeners } = vi.hoisted(() => ({ listeners: new Map<string, Set<() => void>>() }))
vi.mock("@/shared/ws/client", () => ({ wsClient: { on: (event: string, callback: () => void) => {
  if (!listeners.has(event)) listeners.set(event, new Set())
  listeners.get(event)!.add(callback)
  return () => listeners.get(event)!.delete(callback)
} } }))
vi.mock("@/shared/api/http", async (original) => ({
  ...await original<typeof import("@/shared/api/http")>(), http: { get: vi.fn(), post: vi.fn() },
}))

const unread = (count: number): AssistantUnread => ({ unread_count: count, unread_count_is_lower_bound: false })
const full = () => ({ state: "ready", session: { id: "main" }, event_cursor: "cursor", high_water_mark: 10,
  last_seen_sequence: 0, tasks: [], next_task_cursor: null, next_before_sequence: null,
  answers: [{ message_id: "answer", sequence: 10, available: true, display_token: "signed-display" }],
  ...unread(1) }) as unknown as AssistantSnapshot
let client: QueryClient
let count: number
const calls = (url: string) => vi.mocked(http.get).mock.calls.filter(([path]) => path === url)
const tick = (ms = 20) => act(() => vi.advanceTimersByTimeAsync(ms))
const emit = (event: string) => act(() => listeners.get(event)?.forEach((callback) => callback()))
const wrapper = ({ children }: PropsWithChildren) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
function pendingRead() {
  let finish!: (value: AssistantUnread) => void
  const promise = new Promise<AssistantUnread>((resolve) => { finish = resolve })
  return { promise, finish }
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  listeners.clear()
  useAuthStore.setState({ user: { id: "owner" } as never })
  useWorkspaceStore.setState({ currentId: "workspace" })
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  count = 3
  vi.mocked(http.get).mockImplementation(async (url) => {
    if (url === "/api/assistant/unread") return unread(count)
    if (url === "/api/assistant") return full()
    throw new Error(`Unexpected read: ${url}`)
  })
  vi.mocked(http.post).mockResolvedValue({ last_seen_sequence: 10 })
})
afterEach(() => { cleanup(); client.clear(); vi.restoreAllMocks(); vi.useRealTimers() })

it("ordinary sidebar polls only the light endpoint at the existing 15 second interval", async () => {
  const view = renderHook(() => useAssistantSidebarUnread(false, true), { wrapper })
  await tick()
  expect(view.result.current).toEqual({ count: 3, lowerBound: false })
  expect(calls("/api/assistant/unread")).toHaveLength(1)
  expect(calls("/api/assistant")).toHaveLength(0)
  count = 0
  await tick(15_020)
  expect(calls("/api/assistant/unread")).toHaveLength(2)
  expect(view.result.current).toEqual({ count: 0, lowerBound: false })
  expect(new Headers(calls("/api/assistant/unread")[0][1]?.headers).get("X-Workspace-Id")).toBe("workspace")
})

it("main layout and entry share one full key and preserve display-token cursor semantics", async () => {
  const view = renderHook(() => ({ badge: useAssistantSidebarUnread(true, true),
    snapshot: useAssistantSnapshot(), read: useAssistantReadCursor() }), { wrapper })
  await tick()
  expect(calls("/api/assistant")).toHaveLength(1)
  expect(calls("/api/assistant/unread")).toHaveLength(0)
  expect(view.result.current.badge).toEqual({ count: 1, lowerBound: false })
  const answer = view.result.current.snapshot.data!.answers[0]
  await act(async () => { await view.result.current.read.mutateAsync(answer) })
  await tick()
  expect(vi.mocked(http.post).mock.calls[0]).toEqual(["/api/assistant/read-cursor",
    { last_seen_sequence: 10, display_token: "signed-display" }, { signal: undefined, headers: { "X-Workspace-Id": "workspace" } }])
  expect(view.result.current.badge).toEqual({ count: 0, lowerBound: false })
  expect(calls("/api/assistant/unread")).toHaveLength(0)
  await tick(15_020)
  expect(calls("/api/assistant")).toHaveLength(2)
  expect(calls("/api/assistant/unread")).toHaveLength(0)
})

it("hidden sidebar or unresolved session enables neither read, and leaves no hint listener", async () => {
  const view = renderHook(({ enabled }) => useAssistantSidebarUnread(false, enabled),
    { wrapper, initialProps: { enabled: false } })
  await tick(16_000)
  emit("assistant.history.changed"); emit("__connected")
  await tick()
  expect(http.get).not.toHaveBeenCalled()
  expect(view.result.current).toBeUndefined()
  view.rerender({ enabled: true })
  await tick()
  expect(calls("/api/assistant/unread")).toHaveLength(1)
  view.rerender({ enabled: false })
  expect(view.result.current).toBeUndefined()
  emit("assistant.history.changed")
  await tick(16_000)
  expect(calls("/api/assistant/unread")).toHaveLength(1)
})

it.each(["assistant.history.changed", "__connected", "foreground"])(
  "%s rejects a pre-hint response and queues one fresh pass without repeated cancellation", async (event) => {
    const view = renderHook(() => useAssistantSidebarUnread(false, true), { wrapper })
    await tick()
    const delayed = pendingRead()
    vi.mocked(http.get).mockImplementationOnce(() => delayed.promise)
    await tick(15_020)
    const signal = calls("/api/assistant/unread")[1][1]?.signal
    expect(signal?.aborted).toBe(false)
    count = 0
    for (let index = 0; index < 10; index++) {
      if (event === "foreground") {
        vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible")
        act(() => document.dispatchEvent(new Event("visibilitychange")))
      } else emit(event)
    }
    await tick(200)
    expect(view.result.current).toBeUndefined()
    expect(signal?.aborted).toBe(false)
    expect(calls("/api/assistant/unread")).toHaveLength(2)
    await act(async () => delayed.finish(unread(3)))
    expect(view.result.current).toBeUndefined()
    await tick(400)
    expect(calls("/api/assistant/unread")).toHaveLength(3)
    expect(view.result.current).toEqual({ count: 0, lowerBound: false })
    expect(calls("/api/assistant")).toHaveLength(0)
  },
)

it.each(["actor", "workspace"])("a late previous %s response cannot populate the new badge", async (field) => {
  const delayed = pendingRead()
  vi.mocked(http.get).mockImplementationOnce(() => delayed.promise)
  const view = renderHook(() => useAssistantSidebarUnread(false, true), { wrapper })
  await tick()
  const signal = calls("/api/assistant/unread")[0][1]?.signal
  count = 0
  act(() => {
    if (field === "actor") useAuthStore.setState({ user: { id: "peer" } as never })
    else useWorkspaceStore.setState({ currentId: "other-workspace" })
  })
  await tick()
  expect(signal?.aborted).toBe(true)
  expect(view.result.current).toEqual({ count: 0, lowerBound: false })
  await act(async () => delayed.finish(unread(9)))
  await tick()
  expect(view.result.current).toEqual({ count: 0, lowerBound: false })
  const actor = field === "actor" ? "peer" : "owner"
  const workspace = field === "workspace" ? "other-workspace" : "workspace"
  expect(client.getQueryData(assistantKeys.unread(actor, workspace))).toEqual(unread(0))
  expect(new Headers(calls("/api/assistant/unread")[1][1]?.headers).get("X-Workspace-Id")).toBe(workspace)
  expect(calls("/api/assistant")).toHaveLength(0)
})

it("switching to main cancels only the light read and ignores its late response", async () => {
  const delayed = pendingRead()
  vi.mocked(http.get).mockImplementationOnce(() => delayed.promise)
  const view = renderHook(({ main }) => useAssistantSidebarUnread(main, true),
    { wrapper, initialProps: { main: false } })
  await tick()
  const signal = calls("/api/assistant/unread")[0][1]?.signal
  view.rerender({ main: true })
  await tick()
  expect(signal?.aborted).toBe(true)
  expect(view.result.current).toEqual({ count: 1, lowerBound: false })
  await act(async () => delayed.finish(unread(50)))
  await tick(16_000)
  expect(view.result.current).toEqual({ count: 1, lowerBound: false })
  expect(calls("/api/assistant/unread")).toHaveLength(1)
})

it("a failed current-source refresh hides the badge and waits for the next poll", async () => {
  const view = renderHook(() => useAssistantSidebarUnread(false, true), { wrapper })
  await tick()
  vi.mocked(http.get).mockRejectedValueOnce(new ApiError(403, "ASSISTANT_WORKSPACE_FORBIDDEN", "Removed"))
  emit("assistant.history.changed")
  await tick(300)
  expect(view.result.current).toBeUndefined()
  expect(calls("/api/assistant/unread")).toHaveLength(2)
  await tick(1_000)
  expect(calls("/api/assistant/unread")).toHaveLength(2)
  count = 0
  await tick(15_020)
  expect(view.result.current).toEqual({ count: 0, lowerBound: false })
})
