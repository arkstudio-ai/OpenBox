import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { WikiCompile } from "./WikiCompile"
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
let client: QueryClient
let enabled: boolean
const onQueued = vi.fn()
beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  enabled = true
  vi.spyOn(http, "get").mockImplementation((path) =>
    Promise.resolve(
      path.endsWith("capabilities")
        ? {
            enabled,
            model: "configured-model",
            max_memories: 12,
            max_sources: 12,
            max_source_characters: 16000,
          }
        : {
            memories: [
              {
                id: "m1",
                summary: "Selected project rule",
                revision: 2,
                source_count: 1,
                sources: [{ id: "s1", characters: 25 }],
              },
              {
                id: "m2",
                summary: "Unrelated project rule",
                revision: 1,
                source_count: 1,
                sources: [{ id: "s2", characters: 25 }],
              },
            ],
            next_offset: null,
          },
    ),
  )
})
afterEach(() => {
  cleanup()
  client.clear()
  vi.restoreAllMocks()
  onQueued.mockClear()
})
function mount() {
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <WikiCompile projectId="p1" onQueued={onQueued} onClose={() => {}} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}
it("compiles only selected eligible memories, binds scope, and resets consent on input changes", async () => {
  const post = vi.spyOn(http, "post").mockResolvedValue({ id: "j1", status: "pending" })
  mount()
  fireEvent.click(await screen.findByLabelText(/Selected project rule/))
  fireEvent.change(screen.getByLabelText("pageTitle"), { target: { value: "Project guide" } })
  expect((screen.getByRole("button", { name: "generate" }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(screen.getByLabelText("costConsent"))
  fireEvent.change(screen.getByLabelText("pageTitle"), { target: { value: "Updated project guide" } })
  expect((screen.getByLabelText("costConsent") as HTMLInputElement).checked).toBe(false)
  fireEvent.click(screen.getByLabelText("costConsent"))
  fireEvent.click(screen.getByRole("button", { name: "generate" }))
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(
      "/api/memory-wiki/compile",
      expect.objectContaining({
        title: "Updated project guide",
        project_id: "p1",
        memory_ids: ["m1"],
        request_id: expect.any(String),
        confirm_cost: true,
      }),
    ),
  )
  expect(post).toHaveBeenCalledTimes(1)
  expect(onQueued).toHaveBeenCalledWith({ id: "j1", status: "pending" })
})
it("never compiles an empty selection or disabled capability", async () => {
  enabled = false
  const post = vi.spyOn(http, "post")
  mount()
  await screen.findByText("Selected project rule")
  fireEvent.change(screen.getByLabelText("pageTitle"), { target: { value: "Guide" } })
  expect((screen.getByLabelText("costConsent") as HTMLInputElement).disabled).toBe(true)
  expect((screen.getByRole("button", { name: "generate" }) as HTMLButtonElement).disabled).toBe(true)
  expect(post).not.toHaveBeenCalled()
})
