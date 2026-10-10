import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"
import { ApiError, http } from "@/shared/api/http"
import { WikiEditor } from "./WikiEditor"

vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
let client: QueryClient
const initial = {
  id: "p1",
  revision: 3,
  content_hash: "a".repeat(64),
  title: "My guide",
  entries: [{ id: "m1", revision: 7, text: "Original preference", max_length: 2000 }],
}
function mount(pageId: string, saved = vi.fn()) {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <WikiEditor pageId={pageId} onSaved={saved} onClose={vi.fn()} />
    </QueryClientProvider>,
  )
  return saved
}
afterEach(() => {
  cleanup()
  client?.clear()
  vi.restoreAllMocks()
})

it("saves the displayed versions in one action and keeps a conflicting edit intact", async () => {
  vi.spyOn(http, "get").mockResolvedValue(initial)
  const post = vi.spyOn(http, "post").mockRejectedValue(new ApiError(409, "wiki_edit_changed", "Conflict"))
  const saved = mount("p1")
  fireEvent.change(await screen.findByLabelText("consumer.content"), {
    target: { value: "Only on weekends" },
  })
  fireEvent.click(screen.getByRole("button", { name: "consumer.save" }))
  await screen.findByText("consumer.conflict")
  expect(post).toHaveBeenCalledWith(
    "/api/memory-wiki/pages/p1/edit",
    expect.objectContaining({
      expected_revision: 3,
      content_hash: initial.content_hash,
      entries: [{ id: "m1", revision: 7, text: "Only on weekends" }],
    }),
  )
  expect((screen.getByLabelText("consumer.content") as HTMLTextAreaElement).value).toBe("Only on weekends")
  expect(saved).not.toHaveBeenCalled()
  expect(screen.queryByRole("checkbox")).toBeNull()
})

it("does not advance the editor's revision when a background refresh sees another edit", async () => {
  const get = vi.spyOn(http, "get").mockResolvedValue(initial)
  const post = vi.spyOn(http, "post").mockResolvedValue({ id: "p1", status: "updating" })
  mount("p1")
  fireEvent.change(await screen.findByLabelText("consumer.content"), { target: { value: "My unsaved edit" } })
  get.mockResolvedValue({ ...initial, revision: 4, title: "Changed elsewhere" })
  await client.invalidateQueries()
  fireEvent.click(screen.getByRole("button", { name: "consumer.save" }))
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(
      "/api/memory-wiki/pages/p1/edit",
      expect.objectContaining({ expected_revision: 3, title: "My guide" }),
    ),
  )
})
