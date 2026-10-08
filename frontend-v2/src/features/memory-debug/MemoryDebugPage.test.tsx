import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { createMemoryRouter, RouterProvider } from "react-router"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { ApiError, http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { MemoryDebugPage } from "./MemoryDebugPage"

vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
vi.mock("@/shared/lib/format", () => ({
  formatDateTime: (value: string) => value,
  formatNumber: (value: number) => String(value),
}))

const run = {
  id: "run-1",
  request_id: "req-1",
  attempt_id: "attempt-1",
  status: "SUCCEEDED",
  created_at: "2026-10-01T00:00:00Z",
}
const capabilities = { debug_view: true, debug_replay: true }
let client: QueryClient
let gets: string[]

beforeEach(() => {
  capabilities.debug_view = true
  capabilities.debug_replay = true
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  gets = []
  useAuthStore.setState({ user: { id: "memory-user" } as never, isAuthenticated: true })
  useWorkspaceStore.setState({ currentId: "workspace-1" })
  vi.spyOn(http, "get").mockImplementation((path) => {
    gets.push(path)
    if (path.includes("/health"))
      return Promise.resolve({
        capabilities,
        index: { lag: null },
        jobs: { pending: 0 },
        outbox: { pending: 0 },
        wiki: { enabled: false },
      })
    if (path.includes("/runs?")) return Promise.resolve({ runs: [run], capabilities, next_cursor: null })
    return Promise.resolve({
      run: { ...run, id: path.endsWith("run-2") ? "run-2" : "run-1" },
      steps: [
        {
          id: "step-1",
          phase: "route",
          status: "completed",
          reason_code: "explicit_rule",
          duration_ms: 0,
          data: { memory: { choice: "retrieve", confidence: 1 }, task: { choice: "skip", confidence: 1 } },
          usage: { input_tokens: null, output_tokens: 0, estimated_cost: null },
        },
      ],
    })
  })
})
afterEach(() => {
  cleanup()
  client.clear()
  vi.restoreAllMocks()
  useAuthStore.getState().clearAuth()
})

function mount() {
  const router = createMemoryRouter([{ path: "/app/memory-debug/:runId?", element: <MemoryDebugPage /> }], {
    initialEntries: ["/app/memory-debug/run-1"],
  })
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return router
}

it("opens run diagnostics read-only and distinguishes unknown usage from measured zero", async () => {
  const post = vi.spyOn(http, "post")
  mount()
  await screen.findByText("retrieve")
  expect(screen.getAllByText("unknown").length).toBeGreaterThan(0)
  expect(screen.getByText("0")).toBeTruthy()
  expect(screen.getAllByText("status.completed").length).toBeGreaterThan(0)
  expect(post).not.toHaveBeenCalled()
  expect(gets).toContain("/api/memory-debug/runs/run-1")
})

it("stops showing a run's details once a fresh read is refused", async () => {
  mount()
  await screen.findByText("retrieve")
  const get = vi.mocked(http.get).getMockImplementation()!
  vi.mocked(http.get).mockImplementation((path) =>
    path === "/api/memory-debug/runs/run-1"
      ? Promise.reject(new ApiError(403, "forbidden", "Forbidden"))
      : get(path),
  )
  await act(async () => client.invalidateQueries())
  await waitFor(() => expect(screen.queryByText("retrieve")).toBeNull())
  expect(screen.getByRole("alert")).toBeTruthy()
})

it("keeps replay disabled when actual backend capabilities deny it", async () => {
  capabilities.debug_view = false
  capabilities.debug_replay = false
  const post = vi.spyOn(http, "post")
  mount()
  await screen.findByText("debug.disabled")
  expect(((await screen.findByRole("button", { name: "debug.replay" })) as HTMLButtonElement).disabled).toBe(
    true,
  )
  expect(post).not.toHaveBeenCalled()
})

it.each(["source_unavailable_or_changed", "input_unavailable_or_changed"])(
  "explains unavailable bodies and blocks replay preview (%s)",
  async (reason) => {
    const get = vi.mocked(http.get).getMockImplementation()!
    vi.mocked(http.get).mockImplementation((path) =>
      path.endsWith("/runs/run-1")
        ? Promise.resolve({
            run: { ...run, body_available: false, body_unavailable_reason: reason },
            steps: [],
          })
        : get(path),
    )
    const post = vi.spyOn(http, "post")
    mount()
    const hint = await screen.findByText(/debug.bodyUnavailable/)
    expect(hint.textContent).toContain(reason)
    const replay = screen.getByRole("button", { name: "debug.replay" }) as HTMLButtonElement
    expect(replay.disabled).toBe(true)
    fireEvent.click(replay)
    expect(screen.queryByRole("dialog")).toBeNull()
    expect(post).not.toHaveBeenCalled()
  },
)

it("applies request, session, project and status filters through authenticated reads", async () => {
  mount()
  await screen.findByText("retrieve")
  fireEvent.change(screen.getByLabelText("debug.request_id"), { target: { value: "request & exact" } })
  fireEvent.change(screen.getByLabelText("debug.session_id"), { target: { value: "session-1" } })
  fireEvent.change(screen.getByLabelText("debug.project_id"), { target: { value: "project-1" } })
  fireEvent.change(screen.getByLabelText("debug.status"), { target: { value: "failed" } })
  fireEvent.click(screen.getByRole("button", { name: "debug.applyFilters" }))
  await waitFor(() =>
    expect(
      gets.some(
        (path) =>
          path.includes("request_id=request+%26+exact") &&
          path.includes("session_id=session-1") &&
          path.includes("project_id=project-1") &&
          path.includes("status=failed"),
      ),
    ).toBe(true),
  )
})

it("requires a preview and cost consent before submitting exactly one new attempt", async () => {
  const post = vi.spyOn(http, "post").mockImplementation((path) =>
    path.endsWith("/preview")
      ? Promise.resolve({
          preview_id: "preview-1",
          can_submit: true,
          input: { hash: "input-1" },
          scope: { project: "project-1" },
          calls: ["jev", "embedding"],
          cost_estimate: null,
          expires_at: "2099-01-01T00:00:00Z",
        })
      : Promise.resolve({ run_id: "run-2", attempt_id: "attempt-2" }),
  )
  const router = mount()
  fireEvent.click(await screen.findByRole("button", { name: "debug.replay" }))
  expect(post).not.toHaveBeenCalled()
  const submit = screen.getByRole("button", { name: "debug.submitReplay" }) as HTMLButtonElement
  expect(submit.disabled).toBe(true)
  fireEvent.click(screen.getByRole("button", { name: "debug.preview" }))
  await screen.findByText("debug.costConsent")
  expect(submit.disabled).toBe(true)
  expect(post).toHaveBeenCalledTimes(1)
  fireEvent.click(screen.getByLabelText("debug.costConsent"))
  fireEvent.click(submit)
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith("/api/memory-debug/replay", {
      preview_id: "preview-1",
      confirm_cost: true,
    }),
  )
  await waitFor(() => expect(router.state.location.pathname).toBe("/app/memory-debug/run-2"))
  expect(post).toHaveBeenCalledTimes(2)
})

it("invalidates the preview and consent when the selected steps change", async () => {
  const post = vi
    .spyOn(http, "post")
    .mockResolvedValue({ preview_id: "preview-1", can_submit: true, expires_at: "2099-01-01T00:00:00Z" })
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "debug.replay" }))
  fireEvent.click(screen.getByRole("button", { name: "debug.preview" }))
  fireEvent.click(await screen.findByLabelText("debug.costConsent"))
  fireEvent.click(screen.getByLabelText("debug.phase.retrieval"))
  expect((screen.getByRole("button", { name: "debug.submitReplay" }) as HTMLButtonElement).disabled).toBe(
    true,
  )
  expect(screen.queryByLabelText("debug.costConsent")).toBeNull()
  expect(post).toHaveBeenCalledTimes(1)
})

it("does not allow a replay from an expired preview", async () => {
  const post = vi
    .spyOn(http, "post")
    .mockResolvedValue({ preview_id: "expired", can_submit: true, expires_at: "2000-01-01T00:00:00Z" })
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "debug.replay" }))
  fireEvent.click(screen.getByRole("button", { name: "debug.preview" }))
  expect(await screen.findByText("debug.previewExpired")).toBeTruthy()
  expect((screen.getByRole("button", { name: "debug.submitReplay" }) as HTMLButtonElement).disabled).toBe(
    true,
  )
  expect(post).toHaveBeenCalledTimes(1)
})
