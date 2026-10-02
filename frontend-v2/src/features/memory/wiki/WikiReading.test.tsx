import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { afterEach, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { WikiMarkdown } from "./WikiMarkdown"
import { WikiReader } from "./WikiReader"
import { WikiReviews } from "./WikiReviews"
import { pageConnections, wikiExport } from "./content"
import type { WikiPage, WikiCandidate, WikiSummary } from "../wiki-api"
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: "en-US" } }),
}))
const page: WikiPage = {
  id: "p1",
  slug: "guide",
  title: "Guide",
  project_id: "project",
  revision: 2,
  content_hash: "a".repeat(64),
  status: "published",
  body_available: true,
  body: "# Guide\n\n## Working together\n\nUse **weekly** check-ins.\n[source:s1@2]\n\n[[decisions|Decision log]]\n\n`[[decisions]]`\n\n<script>alert(1)</script>\n\n![hidden](https://untrusted.test/track)",
  paragraphs: [
    {
      text: "Use weekly check-ins.",
      citations: [{ source_id: "s1", revision: 2, quote: "We meet every week." }],
    },
  ],
  sources: [{ id: "s1", revision: 2 }],
  source_details: [
    {
      id: "s1",
      revision: 2,
      body: "We meet every week. Decisions are documented.",
      session_id: null,
      kind: "user_note",
      created_at: "2026-10-02T00:00:00Z",
    },
  ],
}
const summaries: WikiSummary[] = [
  {
    id: "p2",
    slug: "decisions",
    title: "Decisions",
    project_id: "project",
    revision: 1,
    status: "published",
    body_available: true,
    excerpt: "",
    source_count: 1,
    source_ids: ["s1"],
  },
]
let client: QueryClient | undefined
function mount(node: React.ReactNode) {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  )
}
afterEach(() => {
  cleanup()
  client?.clear()
  vi.restoreAllMocks()
})
it("renders Markdown, scope-resolved wiki links and citations while blocking raw HTML and remote images", () => {
  const citation = vi.fn()
  const { container } = mount(<WikiMarkdown page={page} pages={summaries} onCitation={citation} />)
  expect(screen.getByRole("heading", { name: "Working together" })).toBeTruthy()
  expect(container.querySelector("strong")?.textContent).toBe("weekly")
  expect(screen.getByRole("link", { name: "Decision log" }).getAttribute("href")).toBe(
    "/app/wiki/p2?project=project",
  )
  expect(screen.getByText("[[decisions]]").tagName).toBe("CODE")
  expect(container.querySelector("script, img")).toBeNull()
  fireEvent.click(screen.getByRole("button", { name: "citationNumber" }))
  expect(citation).toHaveBeenCalledWith(0)
})
it("opens source evidence and related pages through read-only API requests", async () => {
  vi.spyOn(http, "get").mockImplementation((url) =>
    Promise.resolve(url.includes("/relations") ? { relations: [], next_offset: null } : page),
  )
  const post = vi.spyOn(http, "post")
  Element.prototype.scrollIntoView = vi.fn()
  mount(<WikiReader pageId="p1" pages={summaries} projectId="project" onEdit={() => {}} />)
  fireEvent.click(await screen.findByRole("button", { name: "citationNumber" }))
  expect(screen.getByText("We meet every week.")).toBeTruthy()
  fireEvent.click(screen.getByText("sourceOriginal"))
  expect(screen.getByText("We meet every week. Decisions are documented.")).toBeTruthy()
  expect(screen.getByRole("link", { name: "Decisions" })).toBeTruthy()
  expect(post).not.toHaveBeenCalled()
})
it("withholds stale bodies, evidence, relationships and exports even if a cached payload contains them", async () => {
  const stale = { ...page, status: "stale", body_available: false }
  vi.spyOn(http, "get").mockResolvedValue(stale)
  mount(<WikiReader pageId="p1" pages={summaries} projectId="project" onEdit={() => {}} />)
  await screen.findByRole("heading", { name: "consumer.updating" })
  expect(screen.queryByText("We meet every week.")).toBeNull()
  expect(screen.queryByText("Working together")).toBeNull()
  expect((screen.getByRole("button", { name: "export" }) as HTMLButtonElement).disabled).toBe(true)
  expect(wikiExport(stale)).toBe("")
  expect(pageConnections([{ ...summaries[0], id: "p1", body_available: false }, summaries[0]])).toEqual([])
})
it("distinguishes a reconciled record from the original user correction and links its conversation", async () => {
  vi.spyOn(http, "get").mockResolvedValue({
    ...page,
    source_details: [
      {
        ...page.source_details![0],
        kind: "verified_memory_revision",
        changes: [{ body: "Starting next month we meet on Tuesdays.", session_id: "correction-session" }],
      },
    ],
  })
  Element.prototype.scrollIntoView = vi.fn()
  mount(<WikiReader pageId="p1" pages={[]} projectId="project" onEdit={() => {}} />)
  fireEvent.click(await screen.findByRole("button", { name: "citationNumber" }))
  expect(screen.getByRole("button", { name: /correctedSource/ })).toBeTruthy()
  expect(screen.getByText("Starting next month we meet on Tuesdays.")).toBeTruthy()
  expect(screen.getByRole("link", { name: "sourceConversation" }).getAttribute("href")).toBe(
    "/app/s/correction-session",
  )
})
it("approves only the exact reviewed candidate and target versions", async () => {
  const candidate: WikiCandidate = {
    ...page,
    id: "c1",
    status: "pending",
    candidate_hash: "c".repeat(64),
    expected_target_revision: 2,
    expected_target_hash: page.content_hash,
    model: "model",
    usage: {},
  }
  const post = vi.spyOn(http, "post").mockResolvedValue(page)
  const published = vi.fn()
  mount(<WikiReviews candidates={[candidate]} pages={[]} enabled onPublished={published} />)
  fireEvent.click(screen.getByRole("button", { name: "approve" }))
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(
      "/api/memory-wiki/candidates/c1/approve",
      expect.objectContaining({
        candidate_hash: candidate.candidate_hash,
        candidate_revision: 2,
        expected_target_revision: 2,
        expected_target_hash: page.content_hash,
      }),
    ),
  )
  expect(published).toHaveBeenCalledWith(page)
})

it("keeps a real non-first source revision and groups distinct excerpts under one source number", () => {
  const actual: WikiPage = {
    ...page,
    body: "A.\n[source:s1@16] [source:s1@16]",
    paragraphs: [
      {
        text: "A.",
        citations: [
          { source_id: "s1", revision: 16, content_hash: "hash", quote: "First excerpt" },
          { source_id: "s1", revision: 16, content_hash: "hash", quote: "Second excerpt" },
        ],
      },
    ],
  }
  const onCitation = vi.fn()
  mount(<WikiMarkdown page={actual} onCitation={onCitation} />)
  expect(screen.getAllByRole("button", { name: "citationNumber" })).toHaveLength(1)
  fireEvent.click(screen.getByRole("button", { name: "citationNumber" }))
  expect(onCitation).toHaveBeenCalledWith(0)
  expect(wikiExport(actual)).toContain("[source:s1@16]\n\n> First excerpt\n\n> Second excerpt")
})
it("does not resolve escaped markup, code, ambiguous slugs or pages in another project", () => {
  const actual = {
    ...page,
    body: "\\[\\[decisions\\]\\]\n\n[outer [[decisions]]](https://example.org)\n\n[[decisions]]\n\n```\n[[decisions]]\n```",
  }
  mount(<WikiMarkdown page={actual} pages={[{ ...summaries[0], project_id: "other-project" }]} />)
  expect(screen.getAllByRole("link")).toHaveLength(1)
  expect(screen.getByRole("link").getAttribute("href")).toBe("https://example.org")
})

it("cannot publish an unavailable candidate or expose its old evidence", () => {
  const candidate: WikiCandidate = {
    ...page,
    id: "stale-candidate",
    status: "pending",
    body_available: false,
    candidate_hash: "c".repeat(64),
    expected_target_revision: 0,
    expected_target_hash: null,
    model: "model",
    usage: {},
  }
  const post = vi.spyOn(http, "post")
  mount(<WikiReviews candidates={[candidate]} pages={[]} enabled onPublished={() => {}} />)
  expect((screen.getByRole("button", { name: "approve" }) as HTMLButtonElement).disabled).toBe(true)
  expect(screen.queryByText("We meet every week.")).toBeNull()
  expect(post).not.toHaveBeenCalled()
})

it("resolves OKF paths to reviewed local pages and sources, leaving unreviewed paths inert", () => {
  mount(
    <WikiMarkdown
      page={{
        ...page,
        exchange_path: "concepts/guide.md",
        exchange_links: {
          "concepts/decisions.md": { page_id: "p2" },
          "references/notes.md": { source_id: "s1" },
        },
        body: "[Reviewed](decisions.md) [Evidence](/references/notes.md) [Pending](pending.md) [Public](https://example.org)",
      }}
    />,
  )
  expect(screen.getByRole("link", { name: "Reviewed" }).getAttribute("href")).toBe(
    "/app/wiki/p2?project=project",
  )
  expect(screen.getByRole("link", { name: "Evidence" }).getAttribute("href")).toBe("#wiki-import-source-s1")
  expect(screen.queryByRole("link", { name: "Pending" })).toBeNull()
  expect(screen.getByRole("link", { name: "Public" }).getAttribute("href")).toBe("https://example.org")
})
