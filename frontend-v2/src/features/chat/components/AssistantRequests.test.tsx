import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { MemoryRouter } from "react-router"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { ApiError, http } from "@/shared/api/http"
import { AssistantRequests } from "./AssistantRequests"
import { AssistantTopbarActions } from "./AssistantTasks"
import { useAssistantTasksPanel } from "../stores/assistant-tasks-panel"

const { listeners } = vi.hoisted(() => ({ listeners: new Map<string, Set<() => void>>() }))
vi.mock("@/shared/ws/client", () => ({ wsClient: { on: (event: string, callback: () => void) => {
  if (!listeners.has(event)) listeners.set(event, new Set())
  listeners.get(event)!.add(callback)
  return () => listeners.get(event)!.delete(callback)
} } }))
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
vi.mock("./QuestionDock", () => ({ QuestionDock: ({ request }: { request: { id: string } }) => <p>{request.id}</p> }))
vi.mock("./PermissionCard", () => ({ PermissionCard: ({ request }: { request: { id: string } }) => <p>{request.id}</p> }))
vi.mock("../api/assistant-watch", () => ({ useAssistantWatch: () => ({ data: { items: [], has_more: false }, isPending: false }) }))
const item = { id: "question-1", session_id: "execution", task_title: "A task", questions: [] }
let client: QueryClient
beforeEach(() => {
  listeners.clear()
  localStorage.clear()
  useAssistantTasksPanel.setState({ open: false })
  useAuthStore.setState({ user: { id: "owner" } as never })
  useWorkspaceStore.setState({ currentId: "workspace" })
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  vi.spyOn(http, "get").mockImplementation(async (url) => url.includes("/requests/waiting") ? { items: [] } as never
    : url.includes("kind=permission")
    ? { items: [], next_cursor: null, receipts: [] } as never : { items: [item], next_cursor: null, receipts: [
    { command_id: "saved", state: "accepted" }, { command_id: "used", state: "applied" },
    { command_id: "unavailable", state: "failed" }] } as never)
  vi.spyOn(http, "post")
})

it("dismisses the chat hint across remounts while retaining other conversations in My tasks", async () => {
  vi.mocked(http.get).mockImplementation(async (url) => url.includes("/requests/waiting")
    ? { items: [{ id: "waiting-1", session_id: "palette", session_title: "配色讨论",
        questions: [{ header: "", question: "页面用哪种配色？" }] }] } as never
    : { items: [], next_cursor: null, receipts: [] } as never)
  const mountReminder = () => render(<MemoryRouter><QueryClientProvider client={client}>
    <AssistantTopbarActions /><AssistantRequests compact />
  </QueryClientProvider></MemoryRouter>)
  const view = mountReminder()
  await screen.findByText("assistant.requests.reminder")
  expect(screen.queryByText("页面用哪种配色？")).toBeNull()
  fireEvent.click(screen.getByRole("button", { name: "assistant.requests.dismissReminder" }))
  expect(screen.queryByText("assistant.requests.reminder")).toBeNull()
  view.unmount()
  mountReminder()
  expect(screen.queryByText("assistant.requests.reminder")).toBeNull()
  expect(screen.getByText("assistant.taskList.pendingCount").className).toContain("text-dangerink")
  fireEvent.click(screen.getByRole("button", { name: "assistant.taskList.buttonWaiting" }))
  const drawer = screen.getByRole("dialog")
  expect(await within(drawer).findByText("页面用哪种配色？")).toBeTruthy()
  expect(within(drawer).queryByText("assistant.taskList.emptyTitle")).toBeNull()
  const link = within(drawer).getByRole("link", { name: "assistant.requests.answerThere" })
  expect(link.getAttribute("href")).toBe("/app/s/palette")
  fireEvent.click(link)
  expect(screen.queryByRole("dialog")).toBeNull()
  expect(http.post).not.toHaveBeenCalled()
})

it("keeps a dismissed batch hidden after partial resolution, but shows new requests and isolates workspaces", async () => {
  let ids = ["one", "two"]
  vi.mocked(http.get).mockImplementation(async (url) => url.includes("kind=question")
    ? { items: ids.map((id) => ({ ...item, id })), next_cursor: null, receipts: [] } as never
    : { items: [], next_cursor: null, receipts: [] } as never)
  render(<MemoryRouter><QueryClientProvider client={client}><AssistantRequests compact /></QueryClientProvider></MemoryRouter>)
  await screen.findByText("assistant.requests.reminder")
  fireEvent.click(screen.getByRole("button", { name: "assistant.requests.dismissReminder" }))
  ids = ["two"]
  await act(() => client.invalidateQueries())
  expect(screen.queryByText("assistant.requests.reminder")).toBeNull()
  ids = ["two", "three"]
  await act(() => client.invalidateQueries())
  expect(await screen.findByText("assistant.requests.reminder")).toBeTruthy()
  fireEvent.click(screen.getByRole("button", { name: "assistant.requests.dismissReminder" }))
  act(() => useWorkspaceStore.setState({ currentId: "another-workspace" }))
  expect(await screen.findByText("assistant.requests.reminder")).toBeTruthy()
  expect(http.post).not.toHaveBeenCalled()
})
afterEach(() => { cleanup(); client.clear(); vi.restoreAllMocks() })
function mount() {
  return render(<MemoryRouter><QueryClientProvider client={client}><AssistantRequests /></QueryClientProvider></MemoryRouter>)
}

it("shows the original question, says who asks, and only mentions a reply that failed", async () => {
  const { container } = mount()
  expect(await screen.findByText("question-1")).toBeTruthy()
  expect(screen.getByText("assistant.requests.asks")).toBeTruthy()
  expect(screen.getAllByRole("link").every((link) => link.getAttribute("href") === "/app/s/execution")).toBe(true)
  expect(screen.getByText("assistant.requests.failed")).toBeTruthy()
  expect(screen.queryByText("assistant.requests.accepted")).toBeNull()
  expect(screen.queryByText("assistant.requests.applied")).toBeNull()
  // Receipts carry command ids; none of them reach the page.
  for (const id of ["saved", "used", "unavailable"]) expect(container.textContent).not.toContain(id)
  expect(http.post).not.toHaveBeenCalled()
})

it("renders nothing when nothing waits on the user", async () => {
  vi.mocked(http.get).mockImplementation(async (url) => url.includes("/requests/waiting") ? { items: [] } as never
    : { items: [], next_cursor: null, receipts: [{ command_id: "used", state: "applied" }] } as never)
  const { container } = mount()
  await waitFor(() => expect(vi.mocked(http.get).mock.calls.length).toBeGreaterThanOrEqual(3))
  expect(container.textContent).toBe("")
})

it("removes stale cards when the authoritative refresh fails", async () => {
  mount()
  await screen.findByText("question-1")
  vi.mocked(http.get).mockRejectedValue(new ApiError(403, "ASSISTANT_WORKSPACE_FORBIDDEN", "revoked"))
  await client.invalidateQueries({ queryKey: ["assistant", "owner", "workspace"] })
  await waitFor(() => expect(screen.queryByText("question-1")).toBeNull())
  expect(screen.getByRole("alert")).toBeTruthy()
})

it("says which task needs an approval and where it runs", async () => {
  vi.mocked(http.get).mockResolvedValueOnce({ items: [], next_cursor: null, receipts: [] })
    .mockResolvedValueOnce({ items: [{ id: "permission-1", session_id: "execution",
      task_title: "Target task", project_name: "Target project" }], next_cursor: null,
      receipts: [{ command_id: "applying-command", state: "applying", request_kind: "permission" }] })
  const { container } = mount()
  expect(await screen.findByText("permission-1")).toBeTruthy()
  expect(screen.getByText("assistant.requests.needsApproval")).toBeTruthy()
  expect(screen.getByText(/Target project/)).toBeTruthy()
  // An applying reply needs no line of its own, and its command id never shows.
  expect(container.textContent).not.toContain("applying-command")
  expect(http.post).not.toHaveBeenCalled()
})

it.each([
  ["question.asked", "question"], ["question.updated", "question"], ["question.replied", "question"],
  ["question.rejected", "question"], ["question.cancelled", "question"],
  ["permission.asked", "permission"], ["permission.replied", "permission"],
])("refetches only the affected requests when %s arrives", async (event, kind) => {
  mount()
  await screen.findByText("question-1")
  const reads = (which: string) => vi.mocked(http.get).mock.calls.filter(([url]) => url.includes(`kind=${which}`)).length
  const other = kind === "question" ? "permission" : "question"
  const before = { kind: reads(kind), other: reads(other) }
  act(() => listeners.get(event)?.forEach((callback) => callback()))
  await waitFor(() => expect(reads(kind)).toBe(before.kind + 1))
  expect(reads(other)).toBe(before.other)
})

it("stops listening once the requests panel unmounts", async () => {
  const view = mount()
  await screen.findByText("question-1")
  expect([...listeners.values()].some((set) => set.size > 0)).toBe(true)
  view.unmount()
  expect([...listeners.values()].every((set) => set.size === 0)).toBe(true)
})

it("lists questions waiting in the user's other conversations with a link to answer there", async () => {
  vi.mocked(http.get).mockImplementation(async (url) => url.includes("/requests/waiting")
    ? { items: [{ id: "waiting-1", session_id: "palette", session_title: "配色讨论", project_name: "贪吃蛇",
        questions: [{ header: "", question: "页面用哪种配色？" }] }] } as never
    : { items: [], next_cursor: null, receipts: [] } as never)
  mount()
  expect(await screen.findByText("页面用哪种配色？")).toBeTruthy()
  expect(screen.getByText("assistant.requests.askMe")).toBeTruthy()
  expect(screen.getByText("配色讨论 · 贪吃蛇")).toBeTruthy()
  expect(screen.getByRole("link", { name: "assistant.requests.answerThere" }).getAttribute("href")).toBe("/app/s/palette")
})
