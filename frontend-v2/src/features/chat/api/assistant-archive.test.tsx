import type { PropsWithChildren } from "react"
import { act, cleanup, renderHook } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { ApiError, http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { assistantKeys, useAssistantArchive } from "./assistant"
import { chatKeys } from "./keys"

vi.mock("@/shared/api/http", async (original) => ({
  ...await original<typeof import("@/shared/api/http")>(), http: { get: vi.fn(), post: vi.fn() },
}))

const receipt = (revision: number) => ({ command_id: "command", task_id: "task", execution_session_id: "execution",
  task_revision: revision, state: "archived" })
const stale = () => new ApiError(409, "ASSISTANT_TASK_REVISION", "Task changed; read tasks.get again")
let client: QueryClient
beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  useAuthStore.setState({ user: { id: "owner" } as never })
  useWorkspaceStore.setState({ currentId: "workspace" })
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  vi.mocked(http.get).mockImplementation(async (url) => {
    if (url === "/api/assistant/tasks/task") return { task: { id: "task", control_revision: 5 } }
    throw new Error(`Unexpected read: ${url}`)
  })
})
afterEach(() => { cleanup(); client.clear() })
const wrapper = ({ children }: PropsWithChildren) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
const posts = () => vi.mocked(http.post).mock.calls
  .map(([url, body, options]) => ({ url, body: body as { idempotency_key: string; expected_revision: number }, options }))

it("stops watching with the inspected revision, then refreshes the snapshot, task cards and the watch list", async () => {
  vi.mocked(http.post).mockResolvedValue(receipt(4))
  const keys = {
    snapshot: assistantKeys.snapshot("owner", "workspace"),
    task: assistantKeys.task("owner", "workspace", "task"),
    watch: assistantKeys.watch("owner", "workspace"),
    history: chatKeys.messages("owner", "execution"),
  }
  for (const key of Object.values(keys)) client.setQueryData(key, { cached: true })
  const view = renderHook(() => useAssistantArchive(), { wrapper })
  let value: unknown
  await act(async () => { value = await view.result.current.mutateAsync({ taskId: "task", revision: 3 }) })
  expect(value).toEqual(receipt(4))
  expect(posts()).toEqual([{ url: "/api/assistant/tasks/task/archive",
    body: { idempotency_key: expect.any(String), expected_revision: 3 },
    options: { signal: undefined, headers: { "X-Workspace-Id": "workspace" } } }])
  expect(client.getQueryState(keys.snapshot)!.isInvalidated).toBe(true)
  expect(client.getQueryState(keys.task)!.isInvalidated).toBe(true)
  expect(client.getQueryState(keys.watch)!.isInvalidated).toBe(true)
  expect(client.getQueryState(keys.history)!.isInvalidated).toBe(false)
})

it("re-reads the task once on a stale revision and retries with the new one", async () => {
  vi.mocked(http.post).mockRejectedValueOnce(stale()).mockResolvedValueOnce(receipt(6))
  const view = renderHook(() => useAssistantArchive(), { wrapper })
  await act(async () => { await view.result.current.mutateAsync({ taskId: "task", revision: 3 }) })
  expect(vi.mocked(http.get).mock.calls.map(([url]) => url)).toEqual(["/api/assistant/tasks/task"])
  const [first, second] = posts()
  expect([first.body.expected_revision, second.body.expected_revision]).toEqual([3, 5])
  // A new revision is a new command; it must not reuse the refused key.
  expect(second.body.idempotency_key).not.toBe(first.body.idempotency_key)
  expect(client.getQueryData(assistantKeys.task("owner", "workspace", "task"))).toEqual({ task: { id: "task", control_revision: 5 } })
})

it("gives up after one refreshed retry", async () => {
  vi.mocked(http.post).mockRejectedValue(stale())
  const view = renderHook(() => useAssistantArchive(), { wrapper })
  await act(async () => {
    await expect(view.result.current.mutateAsync({ taskId: "task", revision: 3 })).rejects.toMatchObject({ code: "ASSISTANT_TASK_REVISION" })
  })
  expect(posts()).toHaveLength(2)
  expect(http.get).toHaveBeenCalledTimes(1)
})

it("does not retry any other refusal", async () => {
  vi.mocked(http.post).mockRejectedValue(new ApiError(404, "ASSISTANT_TASK_UNAVAILABLE", "Task is unavailable"))
  const view = renderHook(() => useAssistantArchive(), { wrapper })
  await act(async () => {
    await expect(view.result.current.mutateAsync({ taskId: "task", revision: 3 })).rejects.toMatchObject({ status: 404 })
  })
  expect(posts()).toHaveLength(1)
  expect(http.get).not.toHaveBeenCalled()
})

it("reuses the key when a lost response is retried for the same revision", async () => {
  vi.mocked(http.post).mockRejectedValueOnce(new TypeError("response lost")).mockResolvedValueOnce(receipt(4))
  const view = renderHook(() => useAssistantArchive(), { wrapper })
  await act(async () => {
    await expect(view.result.current.mutateAsync({ taskId: "task", revision: 3 })).rejects.toThrow("response lost")
  })
  await act(async () => { await view.result.current.mutateAsync({ taskId: "task", revision: 3 }) })
  const [first, second] = posts()
  expect(second.body).toEqual(first.body)
})
