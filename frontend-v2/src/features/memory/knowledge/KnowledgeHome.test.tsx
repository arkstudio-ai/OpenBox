import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { MemoryRouter, Route, Routes } from "react-router"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { ApiError, http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import type { MemoryProcessing, MemoryRecord, MemoryRevision, MemorySource } from "@/shared/api/memory"
import type { WikiSummary } from "../wiki-api"
import { documentsApi, type KnowledgeDocument } from "../wiki/documents-api"
import { toast } from "@/shared/ui/Toast"
import { KnowledgeWorkspace } from "./KnowledgeWorkspace"

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: "en-US" } }),
}))
vi.mock("@/shared/lib/format", () => ({
  formatDateTime: (value: string) => value,
  formatSince: (value: string) => value,
  formatBytes: (value: number) => String(value),
  formatNumber: (value: number) => String(value),
}))

const memory: MemoryRecord = {
  id: "memory-1",
  summary: "Use Shanghai timezone",
  type: "USER_NOTE",
  scope: "LONG_TERM",
  status: "ACTIVE",
  revision: 3,
  project_id: null,
  updated_at: "2026-10-02T08:00:00Z",
}
const other: MemoryRecord = {
  ...memory,
  id: "memory-2",
  summary: "Weekly report goes out on Friday",
  revision: 1,
}
const topic: WikiSummary = {
  id: "page-1",
  slug: "working-hours",
  title: "Working hours",
  revision: 1,
  status: "published",
  body_available: true,
  excerpt: "## Hours\n\nWe work from **Shanghai**. [source:s1@1]",
  source_count: 1,
  source_ids: ["s1"],
  project_id: null,
  updated_at: "2026-10-02T09:00:00Z",
}
const documentPage: WikiSummary = {
  ...topic,
  id: "doc-page",
  title: "Venue guide",
  excerpt: "Doors open at nine.",
}
const doc: KnowledgeDocument = {
  id: "doc-1",
  filename: "venue-guide.pdf",
  revision: 1,
  status: "ready",
  reason_code: null,
  chunk_count: 1,
  indexed_chunks: 1,
  page_ids: ["doc-page"],
}

let client: QueryClient
let memories: MemoryRecord[]
let documents: KnowledgeDocument[]
let pages: WikiSummary[]
let requests: string[]
let processing: MemoryProcessing
let cleanupPending: number

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  memories = [memory, other]
  processing = { pending: 0, failed: [] }
  cleanupPending = 0
  documents = [doc]
  pages = [topic, documentPage]
  requests = []
  useAuthStore.setState({ user: { id: "memory-user" } as never, isAuthenticated: true })
  useWorkspaceStore.setState({ currentId: "workspace-1" })
  vi.spyOn(http, "get").mockImplementation((path) => {
    requests.push(path)
    if (path === "/api/agent/project")
      return Promise.resolve([
        { id: "p1", name: "Project One" },
        { id: "p2", name: "Project Two" },
      ])
    if (path.endsWith("/capabilities")) return Promise.resolve({ enabled: true })
    if (path.startsWith("/api/memory-wiki/memory-groups"))
      return Promise.resolve({
        groups: [{ id: "g1", title: "Working hours", page_id: "page-1", memory_ids: ["memory-1"] }],
      })
    if (path.startsWith("/api/memory-wiki/library")) {
      const query = new URL(path, "http://test").searchParams.get("query") ?? ""
      return Promise.resolve({
        pages: pages.filter((page) => !query || page.title.toLowerCase().includes(query.toLowerCase())),
        next_offset: null,
      })
    }
    if (path.startsWith("/api/memory-documents"))
      return Promise.resolve({ documents, next_offset: null, cleanup_pending: cleanupPending })
    if (path.endsWith("/sources"))
      return Promise.resolve({
        sources: [
          {
            id: "snapshot-1",
            source_kind: "user_statement",
            session_id: "session-1",
            body: "Original immutable evidence",
            body_available: true,
          },
        ],
      })
    if (path.endsWith("/history"))
      return Promise.resolve({
        revisions: [
          { revision: 2, summary: "Earlier value", body_available: true, reason: "user_corrected" },
        ],
      })
    if (path.startsWith("/api/memories/processing")) return Promise.resolve(processing)
    const single = /^\/api\/memories\/([^/?]+)$/.exec(path)
    if (single && !["settings", "export"].includes(single[1])) {
      const found = memories.find((item) => item.id === decodeURIComponent(single[1]))
      return found
        ? Promise.resolve({ ...found, body_available: true })
        : Promise.reject(new ApiError(404, "", ""))
    }
    if (path.startsWith("/api/memories/settings"))
      return Promise.resolve({ auto_save: true, session_paused: false })
    if (path.endsWith("/cleanup")) return Promise.resolve({ status: "active", stopped: false })
    if (path.startsWith("/api/memories?")) return Promise.resolve({ memories })
    if (path === "/api/memory-wiki/pages/page-1")
      return Promise.resolve({
        ...topic,
        content_hash: "a".repeat(64),
        body: "# Working hours\n\nWe work from Shanghai.",
        paragraphs: [],
        sources: [],
        source_details: [],
      })
    throw new Error("unexpected GET " + path)
  })
})
afterEach(() => {
  cleanup()
  client.clear()
  vi.restoreAllMocks()
  useAuthStore.getState().clearAuth()
})

function mount(entry = "/app/wiki") {
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/app/wiki/:pageId?" element={<KnowledgeWorkspace />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

it("shows memories, topics and files in one place without writes or model calls", async () => {
  const post = vi.spyOn(http, "post")
  mount()
  expect(await screen.findByRole("button", { name: "Use Shanghai timezone" })).toBeTruthy()
  expect(await screen.findByRole("heading", { name: "Working hours" })).toBeTruthy()
  expect(await screen.findByText("venue-guide.pdf")).toBeTruthy()
  // A document's own page is reached through its file, not listed again as a topic.
  expect(screen.queryByRole("heading", { name: "Venue guide" })).toBeNull()
  expect(screen.getByRole("link", { name: "file.read" }).getAttribute("href")).toBe("/app/wiki/doc-page")
  // Markdown and citation markers never leak into a card preview.
  expect(screen.getByText("Hours We work from Shanghai.")).toBeTruthy()
  // The topic card, and the memory's own link to the topic it belongs to.
  expect(
    screen.getAllByRole("link", { name: /Working hours/ }).map((link) => link.getAttribute("href")),
  ).toEqual(["/app/wiki/page-1", "/app/wiki/page-1"])
  expect(post).not.toHaveBeenCalled()
  expect(requests.some((path) => path.includes("/search") || path.includes("/candidates"))).toBe(false)
})

it.each(["reviews", "workflows", "concepts", "organize", "graph", "exchange"])(
  "opens the simple knowledge page for the retired %s link",
  async (view) => {
    const post = vi.spyOn(http, "post")
    mount("/app/wiki?view=" + view)
    await screen.findByRole("button", { name: "Use Shanghai timezone" })
    expect(screen.getByRole("tab", { name: /view.overview/ }).getAttribute("aria-selected")).toBe("true")
    for (const name of ["approve", "reject", "startRun", "workflows", "reviews", "confirm"])
      expect(screen.queryByRole("button", { name })).toBeNull()
    expect(post).not.toHaveBeenCalled()
  },
)

it("moves between views with the arrow keys from a single Tab stop", async () => {
  mount()
  await screen.findByRole("button", { name: "Use Shanghai timezone" })
  const tabs = screen.getAllByRole("tab")
  expect(tabs.map((tab) => tab.tabIndex)).toEqual([0, -1, -1, -1])
  tabs[0].focus()
  fireEvent.keyDown(tabs[0], { key: "ArrowRight" })
  const memories = screen.getByRole("tab", { name: /view.memories/ })
  expect(memories.getAttribute("aria-selected")).toBe("true")
  expect(document.activeElement).toBe(memories)
  fireEvent.keyDown(memories, { key: "ArrowLeft" })
  fireEvent.keyDown(document.activeElement!, { key: "ArrowLeft" })
  expect(screen.getByRole("tab", { name: /view.files/ }).getAttribute("aria-selected")).toBe("true")
  fireEvent.keyDown(document.activeElement!, { key: "Home" })
  expect(screen.getByRole("tab", { name: /view.overview/ }).getAttribute("aria-selected")).toBe("true")
})

it("filters memories locally and searches topic text on the server", async () => {
  const post = vi.spyOn(http, "post")
  mount()
  await screen.findByRole("button", { name: "Weekly report goes out on Friday" })
  fireEvent.change(screen.getByRole("searchbox", { name: "searchLabel" }), { target: { value: "shanghai" } })
  await waitFor(() => expect(requests.some((path) => path.includes("query=shanghai"))).toBe(true))
  await waitFor(() =>
    expect(screen.queryByRole("button", { name: "Weekly report goes out on Friday" })).toBeNull(),
  )
  expect(screen.getByRole("button", { name: "Use Shanghai timezone" })).toBeTruthy()
  expect(screen.getByText("Shanghai").tagName).toBe("MARK")
  expect(post).not.toHaveBeenCalled()
})

it("pages older memories in from the server instead of hiding them", async () => {
  const get = vi.mocked(http.get).getMockImplementation()!
  vi.mocked(http.get).mockImplementation((path) => {
    if (!path.startsWith("/api/memories?")) return get(path)
    requests.push(path)
    return Promise.resolve(
      path.includes("offset=100")
        ? { memories: [other], next_offset: null }
        : { memories: [memory], next_offset: 100 },
    )
  })
  mount("/app/wiki?view=memories")
  await screen.findByRole("button", { name: "Use Shanghai timezone" })
  expect(screen.queryByRole("button", { name: "Weekly report goes out on Friday" })).toBeNull()
  fireEvent.click(screen.getByRole("button", { name: "loadMore" }))
  expect(await screen.findByRole("button", { name: "Weekly report goes out on Friday" })).toBeTruthy()
  expect(requests.some((path) => path.startsWith("/api/memories?") && path.includes("offset=100"))).toBe(true)
  expect(screen.queryByRole("button", { name: "loadMore" })).toBeNull()
})

it("shows what could not be saved in the person's own words, and retries or dismisses just that", async () => {
  processing = {
    pending: 1,
    failed: [
      {
        id: "job-1",
        session_id: "chat-1",
        session_title: "Weekly plans",
        excerpt: "I take guitar on Wednesdays.",
        failed_at: "2026-10-02T15:25:29Z",
      },
    ],
  }
  const post = vi.spyOn(http, "post").mockResolvedValue({ ok: true })
  mount()
  expect(await screen.findByText("processing.pending")).toBeTruthy()
  fireEvent.click(await screen.findByRole("button", { name: "processing.show" }))
  expect(screen.getByText("“I take guitar on Wednesdays.”")).toBeTruthy()
  expect(screen.getByRole("link", { name: /Weekly plans/ }).getAttribute("href")).toBe("/app/s/chat-1")
  fireEvent.click(screen.getByRole("button", { name: "processing.retry" }))
  await waitFor(() => expect(post).toHaveBeenCalledWith("/api/memories/processing/job-1/retry"))
  fireEvent.click(screen.getByRole("button", { name: "processing.dismiss" }))
  await waitFor(() => expect(post).toHaveBeenCalledWith("/api/memories/processing/job-1/dismiss"))
  expect(post).toHaveBeenCalledTimes(2)
})

it("turns automatic saving off from the manage menu", async () => {
  const put = vi.spyOn(http, "put").mockResolvedValue({ auto_save: false, session_paused: false })
  mount()
  await screen.findByRole("button", { name: "Use Shanghai timezone" })
  fireEvent.click(screen.getByRole("button", { name: "manage.title" }))
  fireEvent.click(await screen.findByRole("menuitem", { name: /manage.autoSave/ }))
  await waitFor(() => expect(put).toHaveBeenCalledWith("/api/memories/settings", { auto_save: false }))
})

it("clears every memory in view only after an explicit confirmation", async () => {
  const post = vi.spyOn(http, "post").mockResolvedValue({ forgotten: 2 })
  mount("/app/wiki?project=p1")
  await screen.findByRole("button", { name: "Use Shanghai timezone" })
  fireEvent.click(screen.getByRole("button", { name: "manage.title" }))
  fireEvent.click(await screen.findByRole("menuitem", { name: "manage.clear" }))
  const dialog = within(screen.getByRole("dialog", { name: "manage.clearTitle" }))
  const clear = dialog.getByRole("button", { name: "manage.clearAction" }) as HTMLButtonElement
  expect(clear.disabled).toBe(true)
  fireEvent.click(dialog.getByLabelText("manage.clearConfirm"))
  expect(clear.disabled).toBe(false)
  fireEvent.click(clear)
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith("/api/memories/forget-all", {
      project_id: "p1",
      confirm: "forget-all",
    }),
  )
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull())
})

it("narrows everything to the chosen project", async () => {
  mount()
  await screen.findByRole("button", { name: "Use Shanghai timezone" })
  fireEvent.change(screen.getByRole("combobox", { name: "scope" }), { target: { value: "p2" } })
  await waitFor(() => {
    expect(requests).toContain("/api/memories?limit=100&status=ACTIVE&project_id=p2")
    expect(
      requests.some((path) => path.startsWith("/api/memory-wiki/library") && path.includes("project_id=p2")),
    ).toBe(true)
    expect(requests).toContain("/api/memory-documents?offset=0&project_id=p2")
  })
})

it("reads a memory's sources and history without writing anything", async () => {
  const post = vi.spyOn(http, "post")
  const patch = vi.spyOn(http, "patch")
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "Use Shanghai timezone" }))
  const sheet = within(screen.getByRole("dialog", { name: "detail.title" }))
  expect(await sheet.findByText("Original immutable evidence")).toBeTruthy()
  expect(sheet.getByText("detail.sourceKind.user_statement")).toBeTruthy()
  expect(sheet.getByRole("link", { name: "detail.openChat" }).getAttribute("href")).toBe("/app/s/session-1")
  expect(sheet.getByRole("link", { name: /Working hours/ })).toBeTruthy()
  fireEvent.click(sheet.getByText("detail.history"))
  expect(await sheet.findByText("Earlier value")).toBeTruthy()
  expect(sheet.getByText(/detail.reason.user_corrected/)).toBeTruthy()
  expect(requests).toContain("/api/memories/memory-1/cleanup")
  expect(post).not.toHaveBeenCalled()
  expect(patch).not.toHaveBeenCalled()
})

it("shows a memory as it is now, never the list's older copy", async () => {
  const get = vi.mocked(http.get).getMockImplementation()!
  vi.mocked(http.get).mockImplementation((path) =>
    path === "/api/memories/memory-1"
      ? Promise.resolve({
          ...memory,
          revision: memory.revision + 1,
          summary: "Use Beijing timezone",
          body_available: true,
        })
      : get(path),
  )
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "Use Shanghai timezone" }))
  const sheet = () => within(screen.getByRole("dialog", { name: "detail.title" }))
  expect(await sheet().findByText("Use Beijing timezone")).toBeTruthy()
  expect(sheet().queryByText("Use Shanghai timezone")).toBeNull()
})

it("shows no text when the current read says it is no longer allowed", async () => {
  const get = vi.mocked(http.get).getMockImplementation()!
  vi.mocked(http.get).mockImplementation((path) =>
    path === "/api/memories/memory-1"
      ? Promise.resolve({ ...memory, summary: "", value: {}, body_available: false })
      : get(path),
  )
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "Use Shanghai timezone" }))
  const sheet = within(screen.getByRole("dialog", { name: "detail.title" }))
  expect(await sheet.findByText("detail.unavailable")).toBeTruthy()
  expect(sheet.queryByText("Use Shanghai timezone")).toBeNull()
  expect(sheet.queryByText("Original immutable evidence")).toBeNull()
})

it("explains a corrected memory with the person's own words, replaced wording last", async () => {
  const get = vi.mocked(http.get).getMockImplementation()!
  vi.mocked(http.get).mockImplementation((path) =>
    path.endsWith("/sources")
      ? Promise.resolve({
          sources: [
            {
              id: "said-first",
              source_kind: "user_statement",
              body: null,
              body_available: false,
              superseded: true,
            },
            {
              id: "corrected",
              source_kind: "verified_memory_revision",
              body: "Corrected record",
              body_available: true,
              superseded: false,
              changes: [{ body: "Actually, Shanghai rather than Tokyo", session_id: "session-2" }],
            },
          ],
        })
      : get(path),
  )
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "Use Shanghai timezone" }))
  const sheet = within(screen.getByRole("dialog", { name: "detail.title" }))
  expect(await sheet.findByText("Actually, Shanghai rather than Tokyo")).toBeTruthy()
  expect(sheet.getByText("detail.yourCorrection")).toBeTruthy()
  expect(sheet.getByRole("link", { name: "detail.openChat" }).getAttribute("href")).toBe("/app/s/session-2")
  expect(sheet.queryByText("detail.sourceUnavailable")).toBeNull()
  const items = sheet.getAllByRole("listitem").map((item) => item.textContent ?? "")
  const current = items.findIndex((text) => text.includes("Corrected record"))
  const replaced = items.findIndex((text) => text.includes("detail.supersededSource"))
  expect(current).toBeGreaterThanOrEqual(0)
  expect(replaced).toBeGreaterThan(current)
})

it("sends the shown revision on edit and keeps the person's text on a conflict", async () => {
  const patch = vi
    .spyOn(http, "patch")
    .mockRejectedValue(new ApiError(409, "memory_revision_conflict", "Conflict"))
  mount()
  await screen.findByRole("button", { name: "Use Shanghai timezone" })
  fireEvent.click(screen.getAllByRole("button", { name: "memory.edit" })[0])
  fireEvent.change(screen.getByLabelText("editor.label"), { target: { value: "Use UTC instead" } })
  fireEvent.click(screen.getByRole("button", { name: "editor.save" }))
  await waitFor(() =>
    expect(patch).toHaveBeenCalledWith(
      "/api/memories/memory-1",
      expect.objectContaining({
        summary: "Use UTC instead",
        expected_revision: 3,
        request_id: expect.any(String),
      }),
    ),
  )
  expect(await screen.findByText("editor.conflict")).toBeTruthy()
  expect((screen.getByLabelText("editor.label") as HTMLTextAreaElement).value).toBe("Use UTC instead")
})

it("adds plain text to the scope the person picks, with nothing else to configure", async () => {
  const post = vi.spyOn(http, "post").mockResolvedValue({ ...memory, id: "memory-3" })
  mount("/app/wiki?project=p1")
  await screen.findByRole("button", { name: "Use Shanghai timezone" })
  fireEvent.click(screen.getByRole("button", { name: "addMemory" }))
  const dialog = within(screen.getByRole("dialog", { name: "editor.createTitle" }))
  expect((dialog.getByRole("combobox") as HTMLSelectElement).value).toBe("p1")
  fireEvent.change(dialog.getByRole("combobox"), { target: { value: "" } })
  fireEvent.change(dialog.getByLabelText("editor.label"), { target: { value: "Meetings are on Thursday." } })
  fireEvent.click(dialog.getByRole("button", { name: "editor.save" }))
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(
      "/api/memories",
      expect.objectContaining({ summary: "Meetings are on Thursday.", project_id: null }),
    ),
  )
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull())
  expect(post).toHaveBeenCalledTimes(1)
})

it("explains why a memory with an ID, card or phone number cannot be saved", async () => {
  vi.spyOn(http, "post").mockRejectedValue(
    new ApiError(422, "MEMORY_SENSITIVE_CONTENT", "memory_sensitive_content"),
  )
  mount()
  await screen.findByRole("button", { name: "Use Shanghai timezone" })
  fireEvent.click(screen.getByRole("button", { name: "addMemory" }))
  const dialog = within(screen.getByRole("dialog", { name: "editor.createTitle" }))
  fireEvent.change(dialog.getByLabelText("editor.label"), {
    target: { value: "My ID number is 110101199003071234" },
  })
  fireEvent.click(dialog.getByRole("button", { name: "editor.save" }))
  expect(await dialog.findByText("editor.sensitive")).toBeTruthy()
  expect((dialog.getByLabelText("editor.label") as HTMLTextAreaElement).value).toContain("110101199003071234")
})

it("forgets only the memory unless its original wording is explicitly included", async () => {
  const post = vi.spyOn(http, "post").mockResolvedValue({ status: "stopped_cleanup_pending" })
  mount()
  await screen.findByRole("button", { name: "Use Shanghai timezone" })
  fireEvent.click(screen.getAllByRole("button", { name: "memory.forget" })[0])
  let dialog = within(screen.getByRole("dialog", { name: "forget.title" }))
  await dialog.findByLabelText("forget.clearSources")
  fireEvent.click(dialog.getByRole("button", { name: "forget.confirm" }))
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(
      "/api/memories/memory-1/forget",
      expect.objectContaining({ expected_revision: 3, mode: "memory", source_ids: [] }),
    ),
  )
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull())
  fireEvent.click(screen.getAllByRole("button", { name: "memory.forget" })[0])
  dialog = within(screen.getByRole("dialog", { name: "forget.title" }))
  fireEvent.click(await dialog.findByLabelText("forget.clearSources"))
  fireEvent.click(dialog.getByRole("button", { name: "forget.confirm" }))
  await waitFor(() =>
    expect(post).toHaveBeenLastCalledWith(
      "/api/memories/memory-1/forget",
      expect.objectContaining({ mode: "sources", source_ids: ["snapshot-1"] }),
    ),
  )
  // Never a chat, session or project deletion — only the memory API.
  expect(post.mock.calls.every(([path]) => path.startsWith("/api/memories/"))).toBe(true)
})

it("shows no old text once a memory is forgotten, even when a slow read lands later", async () => {
  const get = vi.mocked(http.get).getMockImplementation()!
  const lateSource = deferred<{ sources: MemorySource[] }>()
  const history = deferred<{ revisions: MemoryRevision[] }>()
  let forgotten = false
  let cleaned = false
  vi.mocked(http.get).mockImplementation((path) => {
    if (!forgotten) return get(path)
    if (path.endsWith("/sources")) return lateSource.promise
    if (path.endsWith("/history")) return history.promise
    if (path.endsWith("/cleanup"))
      return Promise.resolve({ status: cleaned ? "cleaned" : "stopped_cleanup_pending", stopped: true })
    if (path.startsWith("/api/memories?")) return Promise.resolve({ memories: [other] })
    return get(path)
  })
  const post = vi.spyOn(http, "post").mockImplementation(() => {
    forgotten = true
    return Promise.resolve({ ok: true, status: "stopped_cleanup_pending", original_chat_deleted: false })
  })
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "Use Shanghai timezone" }))
  let sheet = within(screen.getByRole("dialog", { name: "detail.title" }))
  await sheet.findByText("Original immutable evidence")
  fireEvent.click(sheet.getByRole("button", { name: "memory.forget" }))
  fireEvent.click(
    within(screen.getByRole("dialog", { name: "forget.title" })).getByRole("button", {
      name: "forget.confirm",
    }),
  )
  // The sheet is rebuilt for the forgotten state; look it up afresh.
  await screen.findByText("detail.cleanupPending")
  sheet = within(screen.getByRole("dialog", { name: "detail.title" }))
  const assertNoOldBodies = () => {
    expect(screen.queryByText(memory.summary)).toBeNull()
    expect(screen.queryByText("Original immutable evidence")).toBeNull()
    expect(screen.queryByText("Earlier value")).toBeNull()
  }
  assertNoOldBodies()
  await act(async () =>
    lateSource.resolve({
      sources: [{ id: "snapshot-1", body: "Original immutable evidence", body_available: true }],
    }),
  )
  await act(async () =>
    history.resolve({
      revisions: [{ revision: 2, summary: "Earlier value", body_available: true, reason: "x" }],
    }),
  )
  assertNoOldBodies()
  expect(sheet.queryByRole("button", { name: "memory.edit" })).toBeNull()
  cleaned = true
  fireEvent.click(sheet.getByRole("button", { name: "detail.checkAgain" }))
  await sheet.findByText("detail.cleanupDone")
  assertNoOldBodies()
  expect(post).toHaveBeenCalledTimes(1)
  expect(post).toHaveBeenCalledWith(
    "/api/memories/memory-1/forget",
    expect.objectContaining({ expected_revision: 3, mode: "memory" }),
  )
})

it("never shows a memory's text when current authority reports it stopped", async () => {
  const get = vi.mocked(http.get).getMockImplementation()!
  vi.mocked(http.get).mockImplementation((path) =>
    path.endsWith("/cleanup") ? Promise.resolve({ status: "cleaned", stopped: true }) : get(path),
  )
  const post = vi.spyOn(http, "post")
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "Use Shanghai timezone" }))
  const sheet = within(screen.getByRole("dialog", { name: "detail.title" }))
  await sheet.findByText("detail.cleanupDone")
  expect(sheet.queryByText(memory.summary)).toBeNull()
  expect(sheet.queryByText("Original immutable evidence")).toBeNull()
  expect(sheet.queryByText("Earlier value")).toBeNull()
  expect(post).not.toHaveBeenCalled()
})

it("drops open details and drafts when the workspace changes", async () => {
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "Use Shanghai timezone" }))
  await screen.findByText("Original immutable evidence")
  fireEvent.keyDown(window, { key: "Escape" })
  fireEvent.click(screen.getByRole("button", { name: "addMemory" }))
  fireEvent.change(screen.getByLabelText("editor.label"), { target: { value: "Private workspace draft" } })
  act(() => useWorkspaceStore.setState({ currentId: "workspace-2" }))
  await waitFor(() => {
    expect(screen.queryByText("Original immutable evidence")).toBeNull()
    expect(screen.queryByRole("dialog")).toBeNull()
  })
  expect(
    client
      .getQueryCache()
      .getAll()
      .some((query) => query.queryKey[2] === "workspace-2"),
  ).toBe(true)
})

it("uploads dropped files to the scope in view and stops oversized ones first", async () => {
  const upload = vi
    .spyOn(documentsApi, "upload")
    .mockResolvedValue({ ...doc, id: "doc-2", status: "pending", created: true })
  mount("/app/wiki?view=files&project=p1")
  const zone = (await screen.findByRole("button", { name: /file.dropTitle/ })) as HTMLButtonElement
  await waitFor(() => expect(zone.disabled).toBe(false))
  const file = new File(["text"], "guide.md", { type: "text/markdown" })
  fireEvent.drop(zone, { dataTransfer: { files: [file] } })
  await waitFor(() => expect(upload).toHaveBeenCalledWith(file, "p1"))
  const large = new File(["too large"], "large.pdf")
  Object.defineProperty(large, "size", { value: 11 * 1024 * 1024 })
  fireEvent.drop(zone, { dataTransfer: { files: [large] } })
  expect(await screen.findByRole("alert")).toBeTruthy()
  expect(upload).toHaveBeenCalledTimes(1)
  expect(screen.queryByRole("button", { name: /approve|workflow|confirm/ })).toBeNull()
})

it("keeps a file readable while search is prepared and retries one that failed", async () => {
  documents = [{ ...doc, status: "index_failed" }]
  const retry = vi.spyOn(documentsApi, "retry").mockResolvedValue({ ...doc, status: "indexing" })
  mount("/app/wiki?view=files")
  expect((await screen.findByRole("link", { name: "file.read" })).getAttribute("href")).toBe(
    "/app/wiki/doc-page",
  )
  fireEvent.click(screen.getByRole("button", { name: "file.retry" }))
  await waitFor(() => expect(retry).toHaveBeenCalledWith("doc-1", expect.anything()))
})

it("welcomes someone with nothing saved yet and shows the ways to start", async () => {
  memories = []
  documents = []
  pages = []
  mount()
  expect(await screen.findByText("welcome.title")).toBeTruthy()
  expect(screen.getByRole("link", { name: "welcome.startChat" }).getAttribute("href")).toBe("/app")
  expect(screen.getAllByRole("button", { name: "addMemory" }).length).toBeGreaterThan(0)
})

it("returns from a page to the exact view it was opened from", async () => {
  mount("/app/wiki?view=topics&q=work")
  fireEvent.click(await screen.findByRole("link", { name: /Working hours/ }))
  expect(await screen.findByRole("heading", { level: 1, name: "Working hours" })).toBeTruthy()
  expect(screen.getByRole("link", { name: "title" }).getAttribute("href")).toBe(
    "/app/wiki?view=topics&q=work",
  )
})

it("deletes a file only after it is confirmed, and only that file", async () => {
  const remove = vi.spyOn(documentsApi, "remove").mockResolvedValue({ ok: true, status: "deleted" })
  mount("/app/wiki?view=files")
  fireEvent.click(await screen.findByRole("button", { name: "file.delete" }))
  let dialog = within(screen.getByRole("dialog", { name: "file.deleteTitle" }))
  expect(dialog.getByText("venue-guide.pdf")).toBeTruthy()
  fireEvent.click(dialog.getByRole("button", { name: "file.cancel" }))
  expect(screen.queryByRole("dialog")).toBeNull()
  expect(remove).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole("button", { name: "file.delete" }))
  dialog = within(screen.getByRole("dialog", { name: "file.deleteTitle" }))
  fireEvent.click(dialog.getByRole("button", { name: "file.deleteConfirm" }))
  await waitFor(() => expect(remove).toHaveBeenCalledWith("doc-1"))
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull())
  expect(remove).toHaveBeenCalledTimes(1)
})

it("does not claim a deleted file is gone while its original is still in storage", async () => {
  vi.spyOn(documentsApi, "remove").mockResolvedValue({ ok: true, status: "deleted", original_cleanup: "pending" })
  const success = vi.spyOn(toast, "success")
  mount("/app/wiki?view=files")
  fireEvent.click(await screen.findByRole("button", { name: "file.delete" }))
  fireEvent.click(
    within(screen.getByRole("dialog", { name: "file.deleteTitle" })).getByRole("button", {
      name: "file.deleteConfirm",
    }),
  )
  await waitFor(() => expect(success).toHaveBeenCalledWith("file.deletedPending"))
})

it("tells the person when deleted originals are still being removed", async () => {
  cleanupPending = 1
  mount("/app/wiki?view=files")
  expect(await screen.findByText("file.cleanupPending")).toBeTruthy()
})
