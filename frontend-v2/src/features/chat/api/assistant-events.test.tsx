import type { ReactNode } from "react"
import { act, cleanup, renderHook, waitFor } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { assistantKeys, useAssistantEvents, type AssistantSnapshot } from "./assistant"
import { chatKeys } from "./keys"

const { listeners } = vi.hoisted(() => ({ listeners: new Map<string, Set<() => void>>() }))
vi.mock("@/shared/ws/client", () => ({ wsClient: { on: (event: string, callback: () => void) => {
  if (!listeners.has(event)) listeners.set(event, new Set())
  listeners.get(event)!.add(callback)
  return () => listeners.get(event)!.delete(callback)
} } }))
let fetchMock: ReturnType<typeof vi.fn>
const initial = { state: "ready", session: { id: "main" }, event_cursor: "initial-cursor",
  high_water_mark: 0, last_seen_sequence: 0, tasks: [], answers: [] } as unknown as AssistantSnapshot
const ready = (position: number, changed = false, more = false) => ({ state: "ready", assistant_session_id: "main",
  next_cursor: `cursor-${position}`, next_sequence: position, high_water_mark: position,
  events: changed ? [{ event_id: "event", sequence: position, kind: "assistant.task.changed" }] : [], has_more: more })
const response = (value: unknown) => new Response(JSON.stringify(value), { status: 200 })
beforeEach(() => {
  listeners.clear()
  fetchMock = vi.fn().mockImplementation(() => Promise.resolve(response(ready(0))))
  vi.stubGlobal("fetch", fetchMock)
  useAuthStore.setState({ user: { id: "owner" } as never, accessToken: null })
  useWorkspaceStore.setState({ currentId: "workspace" })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(assistantKeys.snapshot("owner", "workspace"), initial)
  const invalidate = vi.spyOn(client, "invalidateQueries")
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
  renderHook(() => {
    const workspace = useWorkspaceStore((state) => state.currentId)
    useAssistantEvents(workspace === "workspace" ? "main" : undefined)
  }, { wrapper })
  return { client, invalidate }
}
function connected() { act(() => { listeners.get("__connected")?.forEach((callback) => callback()) }) }

it("replays pages from the snapshot cursor with the captured workspace, then invalidates current views", async () => {
  fetchMock.mockResolvedValueOnce(response(ready(1, true, true))).mockResolvedValueOnce(response(ready(2)))
  const { client, invalidate } = mount()
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
  expect(fetchMock.mock.calls[0][0]).toContain("after=initial-cursor")
  expect(fetchMock.mock.calls[1][0]).toContain("after=cursor-1")
  expect(new Headers(fetchMock.mock.calls[0][1].headers).get("X-Workspace-Id")).toBe("workspace")
  expect(invalidate).toHaveBeenCalledWith({ queryKey: chatKeys.messages("owner", "main") }, { throwOnError: true })
  expect(client.getQueryData<AssistantSnapshot>(assistantKeys.snapshot("owner", "workspace"))!.last_seen_sequence).toBe(0)
  expect(fetchMock.mock.calls.every(([, request]) => (request.method ?? "GET") === "GET")).toBe(true)
})

it("keeps the cursor on a lost response and resumes from it on reconnect", async () => {
  fetchMock.mockRejectedValueOnce(new TypeError("offline")).mockResolvedValueOnce(response(ready(1)))
  mount()
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
  connected()
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
  expect(fetchMock.mock.calls[1][0]).toBe(fetchMock.mock.calls[0][0])
})

it("rebuilds the snapshot on a gap and continues after its matching high-water mark", async () => {
  fetchMock.mockResolvedValueOnce(response({ state: "snapshot_required", reason: "event_gap" }))
    .mockResolvedValueOnce(response({ ...initial, event_cursor: "fresh-cursor", high_water_mark: 10 }))
    .mockResolvedValueOnce(response(ready(10)))
  const { client } = mount()
  const taskKey = assistantKeys.task("owner", "workspace", "older-task")
  const transcriptKey = assistantKeys.transcript("owner", "workspace", "main", ["older-message"])
  client.setQueryData(taskKey, { stale: true })
  client.setQueryData(transcriptKey, { stale: true })
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3))
  expect(fetchMock.mock.calls[1][0]).toMatch(/\/api\/assistant$/)
  expect(fetchMock.mock.calls[2][0]).toContain("after=fresh-cursor")
  expect(client.getQueryData<AssistantSnapshot>(assistantKeys.snapshot("owner", "workspace"))!.high_water_mark).toBe(10)
  expect(client.getQueryState(taskKey)!.isInvalidated).toBe(true)
  expect(client.getQueryState(transcriptKey)!.isInvalidated).toBe(true)
  expect(client.getQueryState(assistantKeys.snapshot("owner", "workspace"))!.isInvalidated).toBe(false)
  expect(fetchMock.mock.calls.every(([, request]) => (request.method ?? "GET") === "GET")).toBe(true)
})

it("does not advance after a required view refresh fails", async () => {
  fetchMock.mockImplementation(() => Promise.resolve(response(ready(1, true))))
  const { invalidate } = mount()
  invalidate.mockRejectedValueOnce(new Error("snapshot unavailable"))
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
  connected()
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
  expect(fetchMock.mock.calls[1][0]).toBe(fetchMock.mock.calls[0][0])
})

it("discards an old workspace response and does not apply its cursor or invalidations", async () => {
  let finish!: (value: Response) => void
  fetchMock.mockImplementationOnce(() => new Promise<Response>((resolve) => { finish = resolve }))
  const { invalidate } = mount()
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
  await act(async () => {
    useWorkspaceStore.setState({ currentId: "other-workspace" })
    finish(response(ready(99, true)))
  })
  expect(invalidate).not.toHaveBeenCalled()
  act(() => { useWorkspaceStore.setState({ currentId: "workspace" }) })
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
  expect(fetchMock.mock.calls[1][0]).toContain("after=initial-cursor")
})

it("coalesces repeated live hints while one replay request is in flight", async () => {
  let finish!: (value: Response) => void
  fetchMock.mockImplementationOnce(() => new Promise<Response>((resolve) => { finish = resolve }))
    .mockResolvedValueOnce(response(ready(1)))
  mount()
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
  for (let i = 0; i < 20; i++) connected()
  expect(fetchMock).toHaveBeenCalledTimes(1)
  await act(async () => { finish(response(ready(1))) })
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
  expect(fetchMock.mock.calls[1][0]).toContain("after=cursor-1")
})
