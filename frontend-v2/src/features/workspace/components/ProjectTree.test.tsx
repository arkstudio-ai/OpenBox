import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { afterEach, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import type { Project, Session } from "@/shared/types/api"
import { ProjectTree } from "./ProjectTree"

vi.mock("react-i18next", async (original) => ({
  ...await original<typeof import("react-i18next")>(),
  useTranslation: () => ({
    t: (key: string, values?: { count?: number }) => (values?.count ? `${key}:${values.count}` : key),
  }),
}))

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const project = { id: "p1", name: "Plans" } as Project
const session = { id: "chat-1", title: "Fruit allergy", project_id: "p1" } as Session

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ProjectTree projects={[project]} sessions={[session]} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

it("says before deleting a chat that what was learned from it stops being used", async () => {
  const get = vi.spyOn(http, "get").mockResolvedValue({ count: 2 })
  mount()
  fireEvent.click(screen.getByRole("button", { name: "sessionActions" }))
  fireEvent.click(screen.getByRole("menuitem", { name: "common:action.delete" }))
  const dialog = within(screen.getByRole("dialog"))
  expect(dialog.getByText("delChatBody")).toBeTruthy()
  expect(await dialog.findByText("delChatMemories:2")).toBeTruthy()
  expect(get).toHaveBeenCalledWith("/api/memories/learned-from/chat-1")
})

it("adds nothing when the chat taught nothing", async () => {
  vi.spyOn(http, "get").mockResolvedValue({ count: 0 })
  mount()
  fireEvent.click(screen.getByRole("button", { name: "sessionActions" }))
  fireEvent.click(screen.getByRole("menuitem", { name: "common:action.delete" }))
  const dialog = within(screen.getByRole("dialog"))
  await new Promise((done) => setTimeout(done, 0))
  expect(dialog.queryByText(/delChatMemories/)).toBeNull()
})

it("opens the project brief from the project's menu", async () => {
  const get = vi.spyOn(http, "get").mockResolvedValue({ id: "b1", project_id: "p1", content: "Goal: plan the trip",
    revision: 2, updated_by: "assistant", updated_at: "2026-10-06T00:00:00Z" })
  mount()
  // The row offers its menu on hover.
  fireEvent.mouseEnter(screen.getAllByRole("button", { name: "Plans" })[0].parentElement!)
  fireEvent.click(screen.getByRole("button", { name: "common:action.more" }))
  fireEvent.click(screen.getByRole("menuitem", { name: "brief.title" }))
  const dialog = within(screen.getByRole("dialog", { name: "brief.title" }))
  expect(((await dialog.findByRole("textbox", { name: "brief.label" })) as HTMLTextAreaElement).value).toBe("Goal: plan the trip")
  expect(get).toHaveBeenCalledWith("/api/projects/p1/brief", expect.anything())
  expect(screen.queryByRole("menu")).toBeNull()
})

it("renames the chosen conversation with a trimmed title", async () => {
  const patch = vi.spyOn(http, "patch").mockResolvedValue({ ...session, title: "Weekend" })
  mount()
  fireEvent.click(screen.getByRole("button", { name: "sessionActions" }))
  fireEvent.click(screen.getByRole("menuitem", { name: "renameChat" }))
  const dialog = within(screen.getByRole("dialog"))
  fireEvent.change(dialog.getByRole("textbox", { name: "chatTitle" }), { target: { value: "  Weekend " } })
  fireEvent.click(dialog.getByRole("button", { name: "common:action.save" }))
  await waitFor(() => expect(patch).toHaveBeenCalledWith("/api/agent/session/chat-1", { title: "Weekend" }))
})

it("submits the explicitly checked conversation IDs and reports retained failures", async () => {
  const post = vi.spyOn(http, "post").mockResolvedValue({ deleted: [], failed: ["chat-1"] })
  mount()
  fireEvent.click(screen.getByRole("button", { name: "manageChats" }))
  fireEvent.click(screen.getByRole("checkbox", { name: "Fruit allergy" }))
  fireEvent.click(screen.getByRole("button", { name: "deleteChats:1" }))
  const dialog = within(screen.getByRole("dialog"))
  expect(dialog.getByText(/delChatBatchMemories/)).toBeTruthy()
  fireEvent.click(dialog.getByRole("button", { name: "deleteChats:1" }))
  await waitFor(() => expect(post).toHaveBeenCalledWith("/api/agent/session/batch-delete", { session_ids: ["chat-1"] }))
  expect((await screen.findByRole("status")).textContent).toContain("chatBatchResult")
  expect((screen.getByRole("checkbox", { name: "Fruit allergy" }) as HTMLInputElement).checked).toBe(true)
})
