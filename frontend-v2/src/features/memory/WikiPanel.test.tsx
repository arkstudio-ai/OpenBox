import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { WikiPanel } from "./WikiPanel"
import type { WikiCandidate, WikiPage } from "./wiki-api"

vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }))

const candidate: WikiCandidate = {
  id: "candidate-1",
  revision: 2,
  candidate_hash: "c".repeat(64),
  title: "Project agreement",
  slug: "project-agreement",
  body: "Grounded candidate paragraph",
  body_available: true,
  status: "pending",
  expected_target_revision: 3,
  expected_target_hash: "t".repeat(64),
  model: "configured-strong-model",
  sources: [{ id: "source-1", revision: 4, content_hash: "source-hash" }],
  paragraphs: [{ text: "Grounded candidate paragraph", citations: [{ quote: "Exact source quote" }] }],
  usage: { input_tokens: 20, estimated_cost: null },
}
let client: QueryClient
let enabled: boolean
let currentCandidates: WikiCandidate[]
let currentPages: WikiPage[]

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  enabled = true
  currentCandidates = []
  currentPages = []
  useAuthStore.setState({ user: { id: "memory-user" } as never, isAuthenticated: true })
  useWorkspaceStore.setState({ currentId: "workspace-1" })
  vi.spyOn(http, "get").mockImplementation((path) => {
    if (path.endsWith("/capabilities")) return Promise.resolve({ enabled, model: candidate.model })
    if (path.includes("/candidates")) return Promise.resolve({ candidates: currentCandidates })
    if (path.includes("/jobs/")) return Promise.resolve({ id: "job-1", status: "pending" })
    return Promise.resolve({ pages: currentPages })
  })
})
afterEach(() => {
  cleanup()
  client.clear()
  vi.restoreAllMocks()
  useAuthStore.getState().clearAuth()
})

function mount(projectId = "project-1") {
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <WikiPanel projectId={projectId} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

it("opens Wiki status read-only and keeps compilation disabled when the capability is off", async () => {
  enabled = false
  const post = vi.spyOn(http, "post")
  mount()
  await screen.findByText("wiki.noPages")
  fireEvent.click(screen.getByText("wiki.compileTitle"))
  fireEvent.change(screen.getByLabelText("wiki.pageTitle"), { target: { value: "Project notes" } })
  fireEvent.change(screen.getByLabelText("wiki.slug"), { target: { value: "project-notes" } })
  expect((screen.getByLabelText("wiki.compileConsent") as HTMLInputElement).disabled).toBe(true)
  expect((screen.getByRole("button", { name: "wiki.compile" }) as HTMLButtonElement).disabled).toBe(true)
  expect(post).not.toHaveBeenCalled()
})

it("requires valid scope-bound input and fresh cost consent before creating one durable job", async () => {
  const post = vi.spyOn(http, "post").mockResolvedValue({ id: "job-1", status: "pending" })
  mount()
  await screen.findByText("wiki.noCandidates")
  fireEvent.click(screen.getByText("wiki.compileTitle"))
  fireEvent.change(screen.getByLabelText("wiki.pageTitle"), { target: { value: "Project notes" } })
  const slug = screen.getByLabelText("wiki.slug") as HTMLInputElement
  fireEvent.change(slug, { target: { value: "project-notes" } })
  const submit = screen.getByRole("button", { name: "wiki.compile" }) as HTMLButtonElement
  expect(slug.getAttribute("pattern")).toBe("[a-z0-9][a-z0-9_\\-]{0,79}")
  expect(submit.disabled).toBe(true)
  fireEvent.click(screen.getByLabelText("wiki.compileConsent"))
  fireEvent.change(slug, { target: { value: "invalid slug!" } })
  expect(submit.disabled).toBe(true)
  expect((screen.getByLabelText("wiki.compileConsent") as HTMLInputElement).checked).toBe(false)
  fireEvent.change(slug, { target: { value: "project-notes" } })
  fireEvent.click(screen.getByLabelText("wiki.compileConsent"))
  fireEvent.click(submit)
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith("/api/memory-wiki/compile", {
      slug: "project-notes",
      title: "Project notes",
      project_id: "project-1",
      request_id: expect.any(String),
      confirm_cost: true,
    }),
  )
  expect(post).toHaveBeenCalledTimes(1)
  expect((await screen.findByRole("link", { name: "inspectTrace" })).getAttribute("href")).toBe(
    "/app/memory-debug?request_id=wiki%3Ajob-1",
  )
  expect(screen.queryByText("wiki.approve")).toBeNull()
})

it("approves the exact displayed immutable candidate and target revision without another model call", async () => {
  currentCandidates = [candidate]
  const post = vi.spyOn(http, "post").mockResolvedValue({ id: "page-1", status: "published" })
  mount()
  expect(await screen.findByText(candidate.body!)).toBeTruthy()
  expect(post).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole("button", { name: "wiki.approve" }))
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith("/api/memory-wiki/candidates/candidate-1/approve", {
      candidate_revision: 2,
      candidate_hash: candidate.candidate_hash,
      expected_target_revision: 3,
      expected_target_hash: candidate.expected_target_hash,
      request_id: expect.any(String),
    }),
  )
  expect(post).toHaveBeenCalledTimes(1)
})

it("withholds stale text and citations and cannot publish an unavailable candidate", async () => {
  const forgotten = "Forgotten original evidence"
  currentCandidates = [
    { ...candidate, body_available: false, body: forgotten, paragraphs: [{ text: forgotten }] },
  ]
  currentPages = [
    {
      ...candidate,
      id: "page-1",
      content_hash: "p".repeat(64),
      status: "stale",
      body_available: false,
      body: forgotten,
      paragraphs: [{ text: forgotten }],
    },
  ]
  const post = vi.spyOn(http, "post")
  mount()
  await screen.findByText("wiki.staleHint")
  expect(screen.queryByText(forgotten, { exact: false })).toBeNull()
  expect((screen.getByRole("button", { name: "wiki.approve" }) as HTMLButtonElement).disabled).toBe(true)
  expect(post).not.toHaveBeenCalled()
})
