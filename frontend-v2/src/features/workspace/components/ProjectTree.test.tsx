import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { afterEach, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import type { Project, Session } from "@/shared/types/api"
import { ProjectTree } from "./ProjectTree"

vi.mock("react-i18next", () => ({
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
        <ProjectTree projects={[project]} sessions={[session]} searching={false} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

it("says before deleting a chat that what was learned from it stops being used", async () => {
  const get = vi.spyOn(http, "get").mockResolvedValue({ count: 2 })
  mount()
  // The row offers deletion on hover.
  fireEvent.mouseEnter(screen.getByRole("link", { name: "Fruit allergy" }).parentElement!)
  fireEvent.click(screen.getByRole("button", { name: "common:action.delete" }))
  const dialog = within(screen.getByRole("dialog"))
  expect(dialog.getByText("delChatBody")).toBeTruthy()
  expect(await dialog.findByText("delChatMemories:2")).toBeTruthy()
  expect(get).toHaveBeenCalledWith("/api/memories/learned-from/chat-1")
})

it("adds nothing when the chat taught nothing", async () => {
  vi.spyOn(http, "get").mockResolvedValue({ count: 0 })
  mount()
  // The row offers deletion on hover.
  fireEvent.mouseEnter(screen.getByRole("link", { name: "Fruit allergy" }).parentElement!)
  fireEvent.click(screen.getByRole("button", { name: "common:action.delete" }))
  const dialog = within(screen.getByRole("dialog"))
  await new Promise((done) => setTimeout(done, 0))
  expect(dialog.queryByText(/delChatMemories/)).toBeNull()
})
