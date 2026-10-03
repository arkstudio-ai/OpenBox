import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { MemoryRouter } from "react-router"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { ApiError, http } from "@/shared/api/http"
import { AssistantRequests } from "./AssistantRequests"

vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
vi.mock("./QuestionDock", () => ({ QuestionDock: ({ request }: { request: { id: string } }) => <p>{request.id}</p> }))
const item = { id: "question-1", session_id: "execution", task_title: "A task", questions: [] }
let client: QueryClient
beforeEach(() => {
  useAuthStore.setState({ user: { id: "owner" } as never })
  useWorkspaceStore.setState({ currentId: "workspace" })
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  vi.spyOn(http, "get").mockResolvedValue({ items: [item], next_cursor: null, receipts: [
    { command_id: "saved", state: "accepted" }, { command_id: "used", state: "applied" },
    { command_id: "unavailable", state: "failed" }] })
  vi.spyOn(http, "post")
})
afterEach(() => { cleanup(); client.clear(); vi.restoreAllMocks() })
function mount() {
  return render(<MemoryRouter><QueryClientProvider client={client}><AssistantRequests /></QueryClientProvider></MemoryRouter>)
}

it("shows the original question and distinct saved/applied/failed receipts without dispatching", async () => {
  mount()
  expect(await screen.findByText("question-1")).toBeTruthy()
  expect(screen.getByRole("link").getAttribute("href")).toBe("/app/s/execution")
  fireEvent.click(screen.getByText("assistant.requests.receipts"))
  expect(screen.getByText("assistant.requests.accepted")).toBeTruthy()
  expect(screen.getByText("assistant.requests.applied")).toBeTruthy()
  expect(screen.getByText("assistant.requests.failed")).toBeTruthy()
  expect(http.post).not.toHaveBeenCalled()
})

it("removes stale cards when the authoritative refresh fails", async () => {
  mount()
  await screen.findByText("question-1")
  vi.mocked(http.get).mockRejectedValue(new ApiError(403, "ASSISTANT_WORKSPACE_FORBIDDEN", "revoked"))
  await client.invalidateQueries({ queryKey: ["assistant", "owner", "workspace"] })
  await waitFor(() => expect(screen.queryByText("question-1")).toBeNull())
  expect(screen.getByRole("alert")).toBeTruthy()
})
