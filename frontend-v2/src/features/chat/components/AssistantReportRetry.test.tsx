import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { MemoryRouter } from "react-router"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { toast } from "@/shared/ui/Toast"
import { assistantKeys, type AssistantTaskView } from "../api/assistant"
import { AssistantTaskCard } from "./AssistantTaskCard"

vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key, i18n: { language: "en-US" } }) }))
vi.mock("@/shared/hooks/useApiErrorMessage", () => ({ useApiErrorMessage: () => () => "Unavailable" }))
vi.mock("@/shared/ui/Toast", () => ({ toast: vi.fn() }))

const value: AssistantTaskView = {
  task: { id: "report-task", title: "Saved original", project_id: "project", execution_session_id: "original-execution",
    desired_state: "running", observed_state: "completed", control_revision: 1, intent_revision: 1, updated_at: "now" },
  execution_session: { id: "original-execution", status: "idle" }, run_binding: null,
  latest_submission: null,
  latest_result: { result_id: "stopped-result", run_id: "original-run", generation: 7, result_message_id: "original-answer",
    outcome: "succeeded", delivery_state: "blocked", report_attempt: 1, assistant_inbox_id: "stopped-inbox",
    processed_message_id: null, last_error_code: "user_stopped", observed_intent_revision: 1, created_at: "now" },
  pending_requests_location: "execution_session",
}
let fetchMock: ReturnType<typeof vi.fn>
let taskView: AssistantTaskView
let post: (request: RequestInit) => Promise<Response>
const clients: QueryClient[] = []
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status })
beforeEach(() => {
  taskView = value
  post = async () => response({ command_id: "retry-receipt", report_attempt: 2, state: "accepted" }, 202)
  fetchMock = vi.fn(async (url: string, request: RequestInit) => {
    if (request.method === "POST") return post(request)
    if (url.includes("/api/assistant/tasks/")) return response(taskView)
    if (url.includes("/api/assistant/watch")) return response({ items: [], has_more: false })
    if (url.includes("/api/assistant/results/")) return response({ ...value.latest_result, offset: 0, next_offset: null,
      source_version: "original-version", sources: [{ session_id: "original-execution", part_id: "original-part",
        text: "The saved execution returned 15.", offset: 0, total_chars: 32 }] })
    throw new Error(`Unexpected request ${url}`)
  })
  vi.stubGlobal("fetch", fetchMock)
  useAuthStore.setState({ user: { id: "report-owner" } as never, accessToken: null })
  useWorkspaceStore.setState({ currentId: "report-workspace" })
  vi.mocked(toast).mockClear()
})
afterEach(() => { cleanup(); clients.splice(0).forEach((client) => client.clear()); vi.unstubAllGlobals() })
function mount(selectedResult?: AssistantTaskView["latest_result"]) {
  const query = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  clients.push(query)
  render(<QueryClientProvider client={query}><MemoryRouter>
    <AssistantTaskCard taskId="report-task" selectedResult={selectedResult ?? undefined} />
  </MemoryRouter></QueryClientProvider>)
  return query
}
function posts() { return fetchMock.mock.calls.filter((call) => call[1].method === "POST") }
async function menuItem(name: string) {
  fireEvent.click(await screen.findByRole("button", { name: "assistant.card.more" }))
  return screen.findByRole("menuitem", { name })
}

it("keeps stopped reports readable across refetch without automatically retrying execution or reporting", async () => {
  const query = mount()
  await screen.findByText("assistant.card.note.reportStopped")
  fireEvent.click(await menuItem("assistant.card.showResult"))
  await screen.findByText("The saved execution returned 15.")
  await query.invalidateQueries({ queryKey: assistantKeys.all("report-owner", "report-workspace") })
  await screen.findByText("The saved execution returned 15.")
  expect(posts()).toHaveLength(0)
  expect(screen.getByText("assistant.status.done")).toBeTruthy()
})

it("uses the same request through the real API after a lost response, including after the card remounts", async () => {
  let attempts = 0
  post = async () => { if (++attempts === 1) throw new TypeError("response lost"); return response({ state: "accepted" }, 202) }
  mount()
  fireEvent.click(await menuItem("assistant.card.retryReport"))
  await waitFor(() => expect(toast).toHaveBeenCalledWith("error", "Unavailable"))
  cleanup()
  mount()
  fireEvent.click(await menuItem("assistant.card.retryReport"))
  await waitFor(() => expect(posts()).toHaveLength(2))
  const [[url, first], [, second]] = posts()
  expect(url).toContain("/api/assistant/results/stopped-result/retry")
  expect(new Headers(first.headers).get("X-Workspace-Id")).toBe("report-workspace")
  expect(JSON.parse(first.body)).toEqual({ idempotency_key: expect.any(String), expected_report_attempt: 1 })
  expect(first.body).toBe(second.body)
  expect(posts().every(([path]) => path.includes("/results/stopped-result/retry"))).toBe(true)
})

it("refreshes a stale attempt after 409 without automatically retrying the replacement", async () => {
  post = async () => {
    taskView = { ...value, latest_result: { ...value.latest_result!, report_attempt: 2,
      delivery_state: "accepted", last_error_code: null } }
    return response({ detail: { code: "ASSISTANT_REPORT_STALE", message: "Changed" } }, 409)
  }
  mount()
  fireEvent.click(await menuItem("assistant.card.retryReport"))
  await waitFor(() => expect(toast).toHaveBeenCalledWith("error", "Unavailable"))
  // The refreshed attempt is no longer blocked, so the menu stops offering a retry.
  await waitFor(() => expect(screen.queryByText("assistant.card.note.reportStopped")).toBeNull())
  fireEvent.click(screen.getByRole("button", { name: "assistant.card.more" }))
  expect(screen.queryByRole("menuitem", { name: "assistant.card.retryReport" })).toBeNull()
  expect(posts()).toHaveLength(1)
  expect(JSON.parse(posts()[0][1].body).expected_report_attempt).toBe(1)
})

it("keeps a selected older result bound when a newer execution result exists", async () => {
  taskView = { ...value, latest_result: { ...value.latest_result!, result_id: "newer-result", report_attempt: 3 } }
  mount(value.latest_result)
  fireEvent.click(await menuItem("assistant.card.retryReport"))
  await waitFor(() => expect(posts()).toHaveLength(1))
  expect(posts()[0][0]).toContain("/results/stopped-result/retry")
  expect(JSON.parse(posts()[0][1].body).expected_report_attempt).toBe(1)
})
