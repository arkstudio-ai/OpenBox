import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { afterEach, expect, it, vi } from "vitest"
import { http } from "@/shared/api/http"
import { WikiOrganization } from "./WikiOrganization"
import { WikiExchange } from "./WikiExchange"
import { WikiWorkflowRun } from "./WikiWorkflowRun"
import type { WorkflowRun } from "./workflow-api"

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: "en-US" } }),
}))
let client: QueryClient
function mount(node: React.ReactNode) {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
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

it("organization browsing is read-only, cost consent resets with budget and binds the preview", async () => {
  const post = vi.spyOn(http, "post").mockResolvedValue({ id: "run" })
  vi.spyOn(http, "get").mockImplementation((url) => {
    if (url.includes("/preview"))
      return Promise.resolve({
        input_hash: "h".repeat(64),
        memory_count: 3,
        changed_count: 2,
        reused_count: 1,
        model: "test",
        skipped: [],
        max_model_calls: 100,
      })
    if (url.includes("/maintenance"))
      return Promise.resolve({
        revision: 0,
        enabled: false,
        call_limit: 20,
        calls_used: 0,
        compile_pages: true,
      })
    return Promise.resolve({ runs: [], next_offset: null })
  })
  mount(<WikiOrganization projectId="project" enabled />)
  await screen.findByText("organizationPreview")
  expect(post).not.toHaveBeenCalled()
  const start = screen.getByRole("button", { name: "startOrganize" }) as HTMLButtonElement
  expect(start.disabled).toBe(true)
  fireEvent.click(screen.getAllByRole("checkbox", { name: "costConsent" })[0])
  expect(start.disabled).toBe(false)
  fireEvent.change(screen.getByRole("spinbutton", { name: "callBudget" }), { target: { value: "7" } })
  expect(start.disabled).toBe(true)
  fireEvent.click(screen.getAllByRole("checkbox", { name: "costConsent" })[0])
  fireEvent.click(start)
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(
      "/api/memory-wiki/organization/runs",
      expect.objectContaining({
        project_id: "project",
        input_hash: "h".repeat(64),
        max_model_calls: 7,
        confirm_cost: true,
      }),
    ),
  )
})

it("import conflicts prevent approval until the server accepts a renamed version", async () => {
  const doc = {
    id: "doc",
    revision: 3,
    content_hash: "d".repeat(64),
    title: "Imported",
    path: "concepts/test.md",
    slug: "test",
    body: "Foreign document",
    status: "pending",
    conflict: true,
    frontmatter: { "x-foreign": { approved: true } },
  }
  vi.spyOn(http, "get").mockImplementation((url) =>
    url.includes("/bundles/bundle")
      ? Promise.resolve({ id: "bundle", project_id: "project", warnings: [], documents: [doc] })
      : Promise.resolve({
          bundles: [{ id: "bundle", created_at: "2026-10-02T00:00:00Z" }],
          next_offset: null,
        }),
  )
  const post = vi
    .spyOn(http, "post")
    .mockResolvedValue({ ...doc, revision: 4, slug: "renamed", conflict: false })
  mount(<WikiExchange projectId="project" />)
  fireEvent.change(await screen.findByRole("combobox", { name: "importHistory" }), {
    target: { value: "bundle" },
  })
  await screen.findByText("Foreign document")
  expect((screen.getByRole("button", { name: "approve" }) as HTMLButtonElement).disabled).toBe(true)
  expect(post).not.toHaveBeenCalled()
  fireEvent.change(screen.getByRole("textbox", { name: "pageAddress" }), { target: { value: "renamed" } })
  fireEvent.click(screen.getByRole("button", { name: "renameImport" }))
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(
      "/api/memory-wiki/exchange/documents/doc/decision",
      expect.objectContaining({
        action: "rename",
        expected_revision: 3,
        content_hash: doc.content_hash,
        slug: "renamed",
      }),
    ),
  )
})

it("workflow approval sends the reviewed output hash and exact run revision without automatic advancement", async () => {
  const run: WorkflowRun = {
    id: "run",
    revision: 8,
    project_id: "project",
    profile_id: "profile",
    workflow_id: "review",
    inputs: {},
    status: "running",
    reason: null,
    stage_index: 0,
    current_stage: "check",
    definition_changed: false,
    definition: {
      schemaVersion: 1,
      profileId: "profile",
      title: "Review",
      entities: {},
      relations: {},
      artifacts: {},
      workflows: { review: { stages: [{ id: "check", reads: [], writes: [], gates: ["human:review"] }] } },
    },
    stages: {
      check: {
        status: "awaiting_review",
        outputs: [],
        output_hash: "exact-output-hash",
        outputs_available: true,
        approvals: {},
        results: [],
        task: null,
      },
    },
    events: [],
  }
  vi.spyOn(http, "get").mockImplementation((url) => {
    if (url.includes("/workflows/run")) return Promise.resolve(run)
    if (url.includes("/records")) return Promise.resolve({ records: [], next_offset: null })
    return Promise.resolve({ pages: [] })
  })
  const post = vi.spyOn(http, "post").mockResolvedValue(run)
  mount(<WikiWorkflowRun runId="run" />)
  fireEvent.click(await screen.findByRole("button", { name: /approveGate/ }))
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(
      "/api/memory-wiki/workflows/run/actions",
      expect.objectContaining({
        expected_revision: 8,
        action: "approve",
        payload: { gate: "human:review", output_hash: "exact-output-hash" },
      }),
    ),
  )
  expect(post.mock.calls).toHaveLength(1)
})
