import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { afterEach, expect, it, vi } from "vitest"
import { WikiUpload } from "./WikiUpload"
import { documentsApi, type KnowledgeDocument } from "./documents-api"

vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
let client: QueryClient
const doc: KnowledgeDocument = {
  id: "d1",
  filename: "guide.pdf",
  revision: 1,
  status: "pending",
  reason_code: null,
  chunk_count: 0,
  indexed_chunks: 0,
  page_ids: [],
}
function mount() {
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <WikiUpload projectId="p1" enabled />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}
afterEach(() => {
  cleanup()
  client?.clear()
  vi.restoreAllMocks()
})

it("uploads to the current project and starts no manual review flow", async () => {
  vi.spyOn(documentsApi, "list").mockResolvedValue({ documents: [doc], next_offset: null })
  const upload = vi.spyOn(documentsApi, "upload").mockResolvedValue(doc)
  mount()
  const file = new File(["PDF text"], "guide.pdf", { type: "application/pdf" })
  fireEvent.change(screen.getByLabelText("documents.upload"), { target: { files: [file] } })
  await screen.findByText("documents.accepted")
  expect(upload).toHaveBeenCalledWith(file, "p1")
  expect(screen.queryByRole("button", { name: /approve|workflow|confirm/ })).toBeNull()
  expect(await screen.findByText("guide.pdf")).toBeTruthy()
})

it("keeps ready files readable while indexing and supports scoped retries", async () => {
  vi.spyOn(documentsApi, "list").mockResolvedValue({
    documents: [{ ...doc, status: "index_failed", page_ids: ["wiki1"] }],
    next_offset: null,
  })
  const retry = vi.spyOn(documentsApi, "retry").mockResolvedValue({ ...doc, status: "indexing" })
  mount()
  const link = await screen.findByRole("link", { name: "readPage" })
  expect(link.getAttribute("href")).toContain("wiki1")
  fireEvent.click(screen.getByRole("button", { name: "retry" }))
  await waitFor(() => expect(retry).toHaveBeenCalledWith("d1", expect.anything()))
})

it("rejects oversized files before upload", async () => {
  vi.spyOn(documentsApi, "list").mockResolvedValue({ documents: [], next_offset: null })
  const upload = vi.spyOn(documentsApi, "upload")
  mount()
  const file = new File(["too large"], "guide.pdf")
  Object.defineProperty(file, "size", { value: 11 * 1024 * 1024 })
  fireEvent.change(screen.getByLabelText("documents.upload"), { target: { files: [file] } })
  await screen.findByRole("alert")
  expect(upload).not.toHaveBeenCalled()
})
