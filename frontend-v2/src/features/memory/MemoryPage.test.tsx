import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { ApiError, http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import type { MemoryRecord, MemoryRevision, MemorySource } from "@/shared/api/memory"
import { MemoryPage } from "./MemoryPage"

vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
vi.mock("@/shared/lib/format", () => ({
  formatDateTime: (value: string) => value,
  formatNumber: (value: number) => String(value),
}))

const memory: MemoryRecord = {
  id: "memory-1",
  summary: "Use Shanghai timezone",
  type: "USER_NOTE",
  scope: "LONG_TERM",
  status: "ACTIVE",
  revision: 3,
  confirmation_status: "CONFIRMED",
}
let client: QueryClient
let current: MemoryRecord
let requests: string[]

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  current = { ...memory }
  requests = []
  useAuthStore.setState({ user: { id: "memory-user" } as never, isAuthenticated: true })
  useWorkspaceStore.setState({ currentId: "workspace-1" })
  vi.spyOn(http, "get").mockImplementation((path) => {
    requests.push(path)
    if (path === "/api/agent/project") return Promise.resolve([{ id: "project-1", name: "Project One" }])
    if (path.endsWith("/sources"))
      return Promise.resolve({
        sources: [
          {
            id: "snapshot-1",
            source_revision: "event-v1",
            body: "Original immutable evidence",
            body_available: true,
            content_hash: "sha256",
          },
        ],
      })
    if (path.endsWith("/history"))
      return Promise.resolve({
        revisions: [{ revision: 2, summary: "Earlier value", body_available: true, reason: "created" }],
      })
    if (path.endsWith("/cleanup")) return Promise.resolve({ status: "active", stopped: false, outbox: [] })
    return Promise.resolve({ memories: [current] })
  })
})
afterEach(() => {
  cleanup()
  client.clear()
  vi.restoreAllMocks()
  useAuthStore.getState().clearAuth()
})

function mount() {
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <MemoryPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

it("reads immutable sources and history without writes or model calls", async () => {
  const post = vi.spyOn(http, "post")
  const patch = vi.spyOn(http, "patch")
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "viewSources" }))
  expect(await screen.findByText("Original immutable evidence")).toBeTruthy()
  expect(screen.getByText("event-v1")).toBeTruthy()
  fireEvent.click(screen.getByRole("tab", { name: "history" }))
  expect(await screen.findByText("Earlier value")).toBeTruthy()
  fireEvent.click(screen.getByRole("tab", { name: "cleanup" }))
  expect(
    await within(screen.getByRole("region", { name: "detail" })).findByText("status.active"),
  ).toBeTruthy()
  expect(requests).toContain("/api/memories/memory-1/sources")
  expect(post).not.toHaveBeenCalled()
  expect(patch).not.toHaveBeenCalled()
})

it("sends the displayed revision on correction and keeps stale edits reviewable", async () => {
  const patch = vi
    .spyOn(http, "patch")
    .mockRejectedValue(new ApiError(409, "memory_revision_conflict", "Conflict"))
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "correct" }))
  fireEvent.change(screen.getByLabelText("content"), { target: { value: "Use UTC instead" } })
  fireEvent.click(screen.getByRole("button", { name: "save" }))
  await waitFor(() =>
    expect(patch).toHaveBeenCalledWith(
      "/api/memories/memory-1",
      expect.objectContaining({
        summary: "Use UTC instead",
        expected_revision: 3,
        request_id: expect.any(String),
      }),
    ),
  )
  expect(await screen.findByText("revisionConflict")).toBeTruthy()
  expect(screen.getByRole("dialog")).toBeTruthy()
  expect((screen.getByLabelText("content") as HTMLTextAreaElement).value).toBe("Use UTC instead")
})

it("keeps review queues and diagnostics out of the consumer memory page", async () => {
  const post = vi.spyOn(http, "post")
  mount()
  await screen.findByRole("tab", { name: "tabs.active" })
  expect(screen.queryByRole("tab", { name: "tabs.candidate" })).toBeNull()
  expect(screen.queryByRole("tab", { name: "tabs.rejected" })).toBeNull()
  expect(screen.queryByRole("link", { name: "debugLink" })).toBeNull()
  expect(post).not.toHaveBeenCalled()
})

it("bounds forgetting to selected source copies and never sends a session deletion", async () => {
  const post = vi.spyOn(http, "post").mockResolvedValue({ status: "stopped_cleanup_pending" })
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "forget" }))
  const dialog = screen.getByRole("dialog")
  const sourceMode = within(dialog).getByLabelText("forgetSelectedSources") as HTMLInputElement
  await waitFor(() => expect(sourceMode.disabled).toBe(false))
  fireEvent.click(sourceMode)
  const confirm = within(dialog).getByRole("button", { name: "forgetConfirm" }) as HTMLButtonElement
  expect(confirm.disabled).toBe(true)
  fireEvent.click(within(dialog).getByLabelText("snapshot-1"))
  fireEvent.click(confirm)
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(
      "/api/memories/memory-1/forget",
      expect.objectContaining({ expected_revision: 3, mode: "sources", source_ids: ["snapshot-1"] }),
    ),
  )
  expect(post.mock.calls.every(([path]) => path.startsWith("/api/memories/"))).toBe(true)
})

it("clears warm and late snapshots on forgetting while retaining cleanup progress", async () => {
  const get = vi.mocked(http.get).getMockImplementation()!
  const lateSource = deferred<{ sources: MemorySource[] }>()
  const sources = deferred<{ sources: MemorySource[] }>()
  const history = deferred<{ revisions: MemoryRevision[] }>()
  let forgotten = false
  let holdSource = false
  let cleaned = false
  vi.mocked(http.get).mockImplementation((path) => {
    if (!forgotten) return holdSource && path.endsWith("/sources") ? lateSource.promise : get(path)
    if (path.endsWith("/sources")) return sources.promise
    if (path.endsWith("/history")) return history.promise
    if (path.endsWith("/cleanup"))
      return Promise.resolve({ status: cleaned ? "cleaned" : "stopped_cleanup_pending", stopped: true })
    if (path.startsWith("/api/memories?"))
      return Promise.resolve({
        memories: path.includes("DEPRECATED") ? [{ ...memory, status: "DEPRECATED" }] : [],
      })
    return get(path)
  })
  const post = vi.spyOn(http, "post").mockImplementation(() => {
    forgotten = true
    return Promise.resolve({
      ok: true,
      status: "stopped_cleanup_pending",
      memory_ids: [memory.id],
      source_ids: [],
      original_chat_deleted: false,
    })
  })
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "viewSources" }))
  await screen.findByText("Original immutable evidence")
  fireEvent.click(screen.getByRole("tab", { name: "history" }))
  await screen.findByText("Earlier value")
  // The forget dialog starts a source read before the mutation takes authority away.
  holdSource = true
  fireEvent.click(screen.getByRole("button", { name: "forget" }))
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "forgetConfirm" }))
  await screen.findByText("status.stopped_cleanup_pending")
  const detail = within(screen.getByRole("region", { name: "detail" }))
  expect(detail.getByRole("tab", { name: "cleanup" }).getAttribute("aria-selected")).toBe("true")
  expect(detail.getByText("memory-1")).toBeTruthy()
  const assertNoOldBodies = () => {
    expect(screen.queryByText(memory.summary)).toBeNull()
    expect(screen.queryByText("Original immutable evidence")).toBeNull()
    expect(screen.queryByText("Earlier value")).toBeNull()
  }
  assertNoOldBodies()
  await act(async () =>
    lateSource.resolve({
      sources: [{ id: "snapshot-1", body: "Original immutable evidence", body_available: true }],
    }),
  )
  assertNoOldBodies()
  fireEvent.click(detail.getByRole("tab", { name: "sources" }))
  assertNoOldBodies()
  await act(async () =>
    sources.resolve({ sources: [{ id: "snapshot-1", body: null, body_available: false }] }),
  )
  await detail.findByText("immutableHint")
  assertNoOldBodies()
  fireEvent.click(detail.getByRole("tab", { name: "history" }))
  assertNoOldBodies()
  await act(async () =>
    history.resolve({
      revisions: [{ revision: 4, summary: memory.summary, body_available: false, reason: "user_forgotten" }],
    }),
  )
  await detail.findByText(/user_forgotten/)
  assertNoOldBodies()
  fireEvent.click(detail.getByRole("tab", { name: "cleanup" }))
  cleaned = true
  fireEvent.click(detail.getByRole("button", { name: "refresh" }))
  await detail.findByText("status.cleaned")
  fireEvent.click(screen.getByRole("tab", { name: "tabs.forgotten" }))
  await screen.findByText("status.deprecated")
  assertNoOldBodies()
  expect(post).toHaveBeenCalledTimes(1)
  expect(post).toHaveBeenCalledWith(
    "/api/memories/memory-1/forget",
    expect.objectContaining({ expected_revision: 3, mode: "memory", source_ids: [] }),
  )
})

it("suppresses an older selected record when current authority reports it stopped", async () => {
  const get = vi.mocked(http.get).getMockImplementation()!
  vi.mocked(http.get).mockImplementation((path) =>
    path.endsWith("/cleanup") ? Promise.resolve({ status: "cleaned", stopped: true }) : get(path),
  )
  const post = vi.spyOn(http, "post")
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "viewSources" }))
  const detail = within(screen.getByRole("region", { name: "detail" }))
  await detail.findByText("immutableHint")
  expect(detail.queryByText(memory.summary)).toBeNull()
  expect(detail.queryByText("Original immutable evidence")).toBeNull()
  expect(detail.getByText("memory-1")).toBeTruthy()
  fireEvent.click(detail.getByRole("tab", { name: "history" }))
  await detail.findByText(/created/)
  expect(detail.queryByText("Earlier value")).toBeNull()
  fireEvent.click(detail.getByRole("tab", { name: "cleanup" }))
  await detail.findByText("status.cleaned")
  expect(detail.getByRole("button", { name: "refresh" })).toBeTruthy()
  expect(post).not.toHaveBeenCalled()
})

it("drops source snapshots and drafts when the workspace changes", async () => {
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "viewSources" }))
  await screen.findByText("Original immutable evidence")
  fireEvent.click(screen.getByRole("button", { name: "create" }))
  fireEvent.change(screen.getByLabelText("content"), { target: { value: "Private workspace draft" } })
  useWorkspaceStore.setState({ currentId: "workspace-2" })
  await waitFor(() => {
    expect(screen.queryByText("Original immutable evidence")).toBeNull()
    expect(screen.queryByRole("dialog")).toBeNull()
  })
  expect(
    client
      .getQueryCache()
      .getAll()
      .some((query) => query.queryKey[2] === "workspace-2"),
  ).toBe(true)
})

it("clarifies the personal search scope and displays the server calendar and timezone basis", async () => {
  const post = vi.spyOn(http, "post").mockResolvedValue({
    request_id: "time-search",
    scope: { project_id: null },
    items: [],
    budget: {},
    degraded_reasons: [],
    time_context: {
      timezone: "Asia/Shanghai",
      expression: "昨天",
      hard_filter_applied: true,
      start_at: "2026-10-01T00:00:00+08:00",
      end_at: "2026-10-02T00:00:00+08:00",
    },
  })
  mount()
  await screen.findByText("searchScopeHint")
  fireEvent.change(screen.getByLabelText("searchLabel"), { target: { value: "昨天的决定" } })
  fireEvent.click(screen.getByRole("button", { name: "search" }))
  const diagnostics = await screen.findByText(/"time_context"/)
  expect(diagnostics.textContent).toContain("Asia/Shanghai")
  expect(diagnostics.textContent).toContain("2026-10-01T00:00:00+08:00")
  expect(post).toHaveBeenCalledWith("/api/memories/search", {
    query: "昨天的决定",
    project_id: null,
    limit: 12,
  })
})
