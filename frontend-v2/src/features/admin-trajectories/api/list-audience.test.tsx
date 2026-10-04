import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, renderHook } from "@testing-library/react"
import type { ReactNode } from "react"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { ApiError } from "@/shared/api/http"
import type { AuthUser } from "@/shared/types/api"
import { LIST_AUDIENCE_REVALIDATE_MS, LIST_AUDIENCE_TIMEOUT_MS } from "../constants/polling"
import { useTrajectoryAccess } from "../stores/access"
import type { SessionPage, SessionRow } from "../types/protocol"
import { EMPTY_LIST_PARAMS, serializeListParams } from "../utils/params"
import { trajectoryApi } from "./endpoints"
import { trajectoryKeys } from "./keys"
import { useSessionList } from "./queries"

vi.mock("./endpoints", () => ({ trajectoryApi: { listSessions: vi.fn(), sessionAudience: vi.fn() } }))
vi.mock("./socket", () => ({ trajectorySocket: { disconnect: vi.fn() } }))
const api = vi.mocked(trajectoryApi)
const params = EMPTY_LIST_PARAMS
let client: QueryClient
const row = (id: string) => ({ session_id: id, user_id: "owner", workspace_id: "ws", title: id }) as SessionRow
const page = (ids: string[], cursor: string | null = null): SessionPage => ({
  items: ids.map(row), next_cursor: cursor, has_more: cursor !== null,
})
const original = page(["newest", "older-private"], "old-tail")
const tick = (ms = 10) => act(() => vi.advanceTimersByTimeAsync(ms))
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}
function seed(value: SessionPage, viewer = "admin#0", listParams = params) {
  client.setQueryData(trajectoryKeys.sessions(viewer, serializeListParams(listParams)), value)
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.resetAllMocks()
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  useTrajectoryAccess.setState({ epoch: 0, denied: null })
  useAuthStore.setState({ user: { id: "admin", role: "admin" } as AuthUser, isAuthenticated: true, isLoading: false })
  api.sessionAudience.mockImplementation(async (targets) => targets.map((target) => target.session_id))
  api.listSessions.mockResolvedValue(original)
  seed(original)
})
afterEach(() => { cleanup(); client.clear(); vi.useRealTimers() })

it("gates a cached page on every mount and keeps ordinary activity from reordering it", async () => {
  const first = deferred<string[]>()
  api.sessionAudience.mockReturnValueOnce(first.promise)
  const hook = renderHook(() => useSessionList(params), { wrapper })
  expect(hook.result.current.data).toBeUndefined()
  expect(hook.result.current.isLoading).toBe(true)
  await act(async () => first.resolve(["newest", "older-private"]))
  expect(hook.result.current.data).toEqual(original)
  await tick(LIST_AUDIENCE_REVALIDATE_MS * 2)
  expect(api.listSessions).not.toHaveBeenCalled()
  expect(hook.result.current.data?.items.map((item) => item.session_id)).toEqual(["newest", "older-private"])
  hook.unmount()
  const later = deferred<string[]>()
  api.sessionAudience.mockReturnValueOnce(later.promise)
  const remount = renderHook(() => useSessionList(params), { wrapper })
  expect(remount.result.current.data).toBeUndefined()
  await act(async () => later.resolve(["newest", "older-private"]))
  expect(remount.result.current.data).toEqual(original)
})

it("detects a revoked non-top row without opening its detail and rebuilds paging metadata", async () => {
  const hook = renderHook(() => useSessionList(params), { wrapper })
  await tick()
  const refreshed = deferred<SessionPage>()
  api.listSessions.mockReturnValueOnce(refreshed.promise)
  api.sessionAudience.mockResolvedValue(["newest"])
  await tick(LIST_AUDIENCE_REVALIDATE_MS)
  expect(hook.result.current.data).toBeUndefined()
  expect(api.listSessions).toHaveBeenCalledTimes(1)
  await act(async () => refreshed.resolve(page(["newest"])))
  await tick()
  expect(hook.result.current.data).toEqual(page(["newest"]))
})

it("hides every old row on authority failure and recovers without changing the snapshot order", async () => {
  const hook = renderHook(() => useSessionList(params), { wrapper })
  await tick()
  api.sessionAudience.mockRejectedValueOnce(new ApiError(503, "down", "internal detail"))
  await tick(LIST_AUDIENCE_REVALIDATE_MS)
  expect(hook.result.current.data).toBeUndefined()
  expect(hook.result.current.error).toMatchObject({ status: 503 })
  expect(hook.result.current.isLoading).toBe(false)
  await tick(LIST_AUDIENCE_REVALIDATE_MS)
  expect(hook.result.current.data).toEqual(original)
  expect(api.listSessions).not.toHaveBeenCalled()
})

it("times out a stalled check and ignores its late permission grant", async () => {
  const hook = renderHook(() => useSessionList(params), { wrapper })
  await tick()
  const pending = deferred<string[]>()
  api.sessionAudience.mockReturnValueOnce(pending.promise)
  await tick(LIST_AUDIENCE_REVALIDATE_MS)
  const signal = api.sessionAudience.mock.calls.at(-1)![1]!
  expect(hook.result.current.data).toEqual(original)
  await tick(LIST_AUDIENCE_TIMEOUT_MS)
  expect(signal.aborted).toBe(true)
  expect(hook.result.current.data).toBeUndefined()
  await act(async () => pending.resolve(["newest", "older-private"]))
  expect(hook.result.current.data).toBeUndefined()
})

it("does not reuse an earlier visit's proof after A -> B -> cached A", async () => {
  const secondParams = { ...params, q: "second" }
  seed(page(["second"]), "admin#0", secondParams)
  const stale = deferred<string[]>()
  api.sessionAudience.mockReturnValueOnce(stale.promise)
  const hook = renderHook(({ current }) => useSessionList(current), { wrapper, initialProps: { current: params } })
  const firstSignal = api.sessionAudience.mock.calls[0][1]!
  hook.rerender({ current: secondParams })
  await tick()
  expect(hook.result.current.data?.items[0].session_id).toBe("second")
  const fresh = deferred<string[]>()
  api.sessionAudience.mockReturnValueOnce(fresh.promise)
  hook.rerender({ current: params })
  expect(firstSignal.aborted).toBe(true)
  await act(async () => stale.resolve(["newest", "older-private"]))
  expect(hook.result.current.data).toBeUndefined()
  await act(async () => fresh.resolve(["newest", "older-private"]))
  expect(hook.result.current.data).toEqual(original)
})

it("fences a late refusal from an old account without locking out the new viewer", async () => {
  const old = deferred<string[]>()
  api.sessionAudience.mockReturnValueOnce(old.promise)
  const hook = renderHook(() => useSessionList(params), { wrapper })
  seed(page(["other-admin"]), "other#0")
  act(() => useAuthStore.setState({ user: { id: "other", role: "admin" } as AuthUser }))
  await tick()
  await act(async () => old.reject(new ApiError(403, "forbidden", "refused")))
  expect(useTrajectoryAccess.getState().denied).toBeNull()
  expect(hook.result.current.data).toEqual(page(["other-admin"]))
})

it("latches a current admin refusal and stops polling", async () => {
  api.sessionAudience.mockRejectedValueOnce(new ApiError(403, "forbidden", "refused"))
  const hook = renderHook(() => useSessionList(params), { wrapper })
  await tick()
  expect(useTrajectoryAccess.getState().denied).toMatchObject({ status: 403 })
  expect(hook.result.current.data).toBeUndefined()
  await tick(LIST_AUDIENCE_REVALIDATE_MS * 3)
  expect(api.sessionAudience).toHaveBeenCalledTimes(1)
})

it("checks in a hidden tab and requires fresh proof immediately on returning to the foreground", async () => {
  const hook = renderHook(() => useSessionList(params), { wrapper })
  await tick()
  const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden")
  await tick(LIST_AUDIENCE_REVALIDATE_MS)
  expect(api.sessionAudience).toHaveBeenCalledTimes(2)
  const fresh = deferred<string[]>()
  api.sessionAudience.mockReturnValueOnce(fresh.promise)
  visibility.mockReturnValue("visible")
  act(() => document.dispatchEvent(new Event("visibilitychange")))
  expect(hook.result.current.data).toBeUndefined()
  await act(async () => fresh.resolve(["newest", "older-private"]))
  expect(hook.result.current.data).toEqual(original)
  visibility.mockRestore()
})

it("manual refresh cannot retain an earlier authority proof while loading", async () => {
  const hook = renderHook(() => useSessionList(params), { wrapper })
  await tick()
  const fresh = deferred<string[]>()
  api.sessionAudience.mockReturnValueOnce(fresh.promise)
  api.listSessions.mockReturnValueOnce(new Promise(() => undefined))
  act(() => { void hook.result.current.refetch() })
  expect(hook.result.current.data).toBeUndefined()
  expect(api.listSessions).toHaveBeenCalledTimes(1)
})
