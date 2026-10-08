import type { PropsWithChildren } from "react"
import { act, cleanup, renderHook } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import type { Session } from "@/shared/types/api"
import { useStreamStore } from "../stores/stream"
import { useSessionQuery } from "./message-actions"

vi.mock("@/shared/api/http", async (original) => ({
  ...await original<typeof import("@/shared/api/http")>(), http: { get: vi.fn() },
}))

const clients: QueryClient[] = []
const key = ["session", "observer-owner", "observer-session"]
const session = (status: Session["status"]): Session => ({ id: "observer-session", user_id: "observer-owner",
  workspace_id: "observer-workspace", kind: "normal", title: "Conversation", agent: "build", model: "test/model",
  status, created_at: "2026-10-05T00:00:00Z", updated_at: "2026-10-05T00:00:00Z" })

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  useAuthStore.setState({ user: { id: "observer-owner" } as never })
  useStreamStore.setState({ status: new Map() })
})
afterEach(() => {
  cleanup()
  clients.splice(0).forEach((client) => client.clear())
  vi.useRealTimers()
})

function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  clients.push(client)
  const wrapper = ({ children }: PropsWithChildren) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
  const mount = (poll = false) => renderHook(() => useSessionQuery("observer-session", { poll }), { wrapper })
  return { client, mount }
}
const tick = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms))

it.each(["normal", "assistant"] as const)("does not refetch as %s history metadata mounts beside the active view", async (kind) => {
  let current = { ...session("busy"), kind }
  vi.mocked(http.get).mockImplementation(async () => ({ ...current }))
  const { mount } = setup()
  const view = mount(true)
  await tick(10)
  const metadata = []
  for (let index = 0; index < 20; index++) {
    metadata.push(mount())
    await tick(10)
  }
  expect(http.get).toHaveBeenCalledTimes(1)
  await tick(1_000)
  expect(http.get).toHaveBeenCalledTimes(2)
  current = { ...current, status: "idle", model: "test/new-model" }
  await tick(1_000)
  expect(http.get).toHaveBeenCalledTimes(3)
  expect(view.result.current.data?.status).toBe("idle")
  expect(metadata.every((hook) => hook.result.current.data?.model === "test/new-model")).toBe(true)
  await tick(3_000)
  expect(http.get).toHaveBeenCalledTimes(3)
})

it("keeps cached metadata subscribed without a status timer after the active view leaves", async () => {
  vi.mocked(http.get).mockResolvedValue(session("busy"))
  const { client, mount } = setup()
  const view = mount(true)
  await tick(10)
  const metadata = mount()
  view.unmount()
  await tick(5_000)
  expect(http.get).toHaveBeenCalledTimes(1)
  vi.mocked(http.get).mockResolvedValue({ ...session("idle"), model: "test/reconnected" })
  await act(async () => { await client.invalidateQueries({ queryKey: key }) })
  await tick(10)
  expect(http.get).toHaveBeenCalledTimes(2)
  expect(metadata.result.current.data?.model).toBe("test/reconnected")
})

it("recovers queued and busy status by polling when live events were missed", async () => {
  vi.mocked(http.get).mockResolvedValueOnce(session("queued")).mockResolvedValueOnce(session("busy"))
    .mockResolvedValue(session("idle"))
  const { mount } = setup()
  const view = mount(true)
  await tick(10)
  expect(view.result.current.data?.status).toBe("queued")
  await tick(1_000)
  expect(view.result.current.data?.status).toBe("busy")
  await tick(1_000)
  expect(view.result.current.data?.status).toBe("idle")
  expect(useStreamStore.getState().status.get("observer-session")).toBe("idle")
  await tick(3_000)
  expect(http.get).toHaveBeenCalledTimes(3)
})

it("does not replace a newer live status with a delayed HTTP response", async () => {
  let finish!: (value: Session) => void
  vi.mocked(http.get).mockImplementation(() => new Promise((resolve) => { finish = resolve }))
  const { mount } = setup()
  mount(true)
  await tick(10)
  useStreamStore.getState().setStatus("observer-session", "idle")
  await act(async () => { finish(session("busy")) })
  expect(http.get).toHaveBeenCalledTimes(1)
  expect(useStreamStore.getState().status.get("observer-session")).toBe("idle")
})
