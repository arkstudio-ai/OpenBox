import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter, Route, Routes } from "react-router"
import { afterEach, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { WikiWorkspace } from "./WikiWorkspace"
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: "en-US" } }),
}))
let client: QueryClient
function mount(view = "library") {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/app/wiki?project=p1&view=" + view]}>
        <Routes>
          <Route path="/app/wiki/:pageId?" element={<WikiWorkspace />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}
afterEach(() => {
  cleanup()
  client?.clear()
  vi.restoreAllMocks()
})
it("browses, searches, paginates and changes projects without any model or write calls", async () => {
  const post = vi.spyOn(http, "post")
  const get = vi.spyOn(http, "get").mockImplementation((url) => {
    if (url.endsWith("/project"))
      return Promise.resolve([
        { id: "p1", name: "Project One" },
        { id: "p2", name: "Project Two" },
      ])
    if (url.endsWith("capabilities")) return Promise.resolve({ enabled: true })
    if (url.includes("/candidates")) return Promise.resolve({ candidates: [] })
    if (url.includes("/library")) {
      const query = new URL(url, "http://test").searchParams
      const offset = Number(query.get("offset"))
      return Promise.resolve({
        pages: [
          {
            id: "page-" + offset,
            title: query.get("query") ? "Matching page" : "Project guide " + offset,
            slug: "guide-" + offset,
            revision: 1,
            status: "published",
            body_available: true,
            source_ids: [],
            source_count: 0,
            excerpt: "A scoped page",
          },
        ],
        next_offset: offset === 0 ? 40 : null,
      })
    }
    throw new Error(url)
  })
  mount()
  await screen.findByText("Project guide 0")
  fireEvent.change(screen.getByRole("textbox", { name: "searchPages" }), { target: { value: "decisions" } })
  fireEvent.click(screen.getByRole("button", { name: "search" }))
  await screen.findByText("Matching page")
  expect(
    get.mock.calls.some(([url]) => url.includes("query=decisions") && url.includes("project_id=p1")),
  ).toBe(true)
  fireEvent.click(screen.getByRole("button", { name: "loadMore" }))
  await waitFor(() => expect(get.mock.calls.some(([url]) => url.includes("offset=40"))).toBe(true))
  fireEvent.change(screen.getByRole("combobox", { name: "scope" }), { target: { value: "p2" } })
  await waitFor(() => expect(get.mock.calls.some(([url]) => url.includes("project_id=p2"))).toBe(true))
  expect(post).not.toHaveBeenCalled()
  expect(get.mock.calls.some(([url]) => url.includes("/candidates"))).toBe(false)
})

it.each(["reviews", "workflows", "concepts", "organize", "graph", "exchange"])(
  "opens the simple library for the old %s link",
  async (view) => {
    vi.spyOn(http, "get").mockImplementation((url) =>
      Promise.resolve(
        url.endsWith("/project")
          ? []
          : url.endsWith("capabilities")
            ? { enabled: true }
            : { pages: [], next_offset: null },
      ),
    )
    const post = vi.spyOn(http, "post")
    mount(view)
    await screen.findByText("consumer.emptyTitle")
    expect(screen.getByRole("button", { name: "consumer.add" })).toBeTruthy()
    for (const name of ["reviews", "workflows", "concepts", "organize", "approve", "startRun"])
      expect(screen.queryByRole("button", { name })).toBeNull()
    expect(post).not.toHaveBeenCalled()
  },
)
