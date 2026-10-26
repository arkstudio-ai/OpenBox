import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { ApiError, http } from "@/shared/api/http"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { ExperienceList } from "./ExperienceList"

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, unknown>) =>
      options?.version ? `${key}:${String(options.version)}` : key,
    i18n: { language: "en-US" },
  }),
}))

const endpoint = "/api/memories/experiences"
const procedure = {
  title: "Check the approved invoice",
  conditions: "Only use the invoice approved for this order.",
  steps: ["Open the approved invoice.", "Compare the order and invoice totals."],
  expected_result: "The approved totals match.",
  failure_boundaries: "Stop when an invoice is missing or has changed.",
  test_plan: "Try one matching and one mismatched invoice.",
}
const reviewNotes = "Checked both the matching and mismatched invoice cases."
type Experience = {
  id: string
  revision: number
  state: "candidate" | "validated" | "enabled" | "retired" | "needs_review"
  content: typeof procedure | null
  sources_available: boolean
  validation: { passed?: boolean; notes?: string }
}

let client: QueryClient
let item: Experience

beforeEach(() => {
  client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  item = {
    id: "experience-1",
    revision: 7,
    state: "candidate",
    content: { ...procedure, steps: [...procedure.steps] },
    sources_available: true,
    validation: {},
  }
  useAuthStore.setState({ user: { id: "experience-user" } as never, isAuthenticated: true })
  useWorkspaceStore.setState({ currentId: "workspace-1" })
  vi.spyOn(http, "get").mockImplementation(async () => ({ items: [item], next_offset: null }))
  vi.spyOn(http, "post").mockRejectedValue(new Error("Unexpected experience write"))
  vi.spyOn(http, "put").mockRejectedValue(new Error("Unexpected experience edit"))
})

afterEach(() => {
  cleanup()
  client.clear()
  vi.restoreAllMocks()
  useAuthStore.getState().clearAuth()
})

function mount() {
  return render(
    <QueryClientProvider client={client}>
      <ExperienceList projectId="project-1" query="" />
    </QueryClientProvider>,
  )
}

function button(name: string) {
  return screen.getByRole("button", { name }) as HTMLButtonElement
}

function fillReview() {
  fireEvent.change(screen.getByLabelText("experience.reviewNotes"), {
    target: { value: reviewNotes },
  })
  fireEvent.click(screen.getByRole("checkbox", { name: "experience.reviewPassed" }))
}

function deferred() {
  let resolve!: (value: unknown) => void
  const promise = new Promise((done) => {
    resolve = done
  })
  return { promise, resolve }
}

it("cannot enable an unreviewed candidate, including after checking the local review checkbox", async () => {
  mount()
  await screen.findByRole("heading", { name: procedure.title })
  expect(http.get).toHaveBeenCalledWith(`${endpoint}?offset=0&project_id=project-1`)
  expect(screen.queryByRole("button", { name: "experience.enable" })).toBeNull()
  expect(button("experience.recordReview").disabled).toBe(true)
  fireEvent.click(screen.getByRole("checkbox", { name: "experience.reviewPassed" }))
  fireEvent.change(screen.getByLabelText("experience.reviewNotes"), { target: { value: "Too short" } })
  expect(button("experience.recordReview").disabled).toBe(true)
  expect(screen.queryByRole("button", { name: "experience.enable" })).toBeNull()
  expect(http.post).not.toHaveBeenCalled()
})

it("sends the reviewed revision and waits for server validation before enabling the next revision", async () => {
  const pending = deferred()
  vi.mocked(http.post).mockReturnValueOnce(pending.promise)
  mount()
  await screen.findByRole("heading", { name: procedure.title })
  fillReview()
  fireEvent.click(button("experience.recordReview"))
  await waitFor(() =>
    expect(http.post).toHaveBeenCalledWith(`${endpoint}/experience-1/review`, {
      expected_revision: 7,
      passed: true,
      notes: reviewNotes,
    }),
  )
  expect(screen.queryByRole("button", { name: "experience.enable" })).toBeNull()
  expect(button("experience.recordReview").disabled).toBe(true)

  item = { ...item, revision: 8, state: "validated", validation: { passed: true, notes: reviewNotes } }
  await act(async () => pending.resolve(item))
  const enable = await screen.findByRole("button", { name: "experience.enable" })
  expect(screen.queryByRole("button", { name: "experience.retire" })).toBeNull()
  vi.mocked(http.post).mockImplementationOnce(async () => {
    item = { ...item, revision: 9, state: "enabled" }
    return item
  })
  fireEvent.click(enable)
  await screen.findByRole("button", { name: "experience.retire" })
  expect(http.post).toHaveBeenLastCalledWith(`${endpoint}/experience-1/enable`, { expected_revision: 8 })
  expect(screen.getByText(/experience\.state\.enabled/)).toBeTruthy()
})

it("edits the complete procedure with expected_revision and returns to review after the server saves it", async () => {
  item = { ...item, state: "validated", validation: { passed: true, notes: reviewNotes } }
  const revised = {
    ...procedure,
    title: "Check the revised invoice",
    steps: ["Read the current approved invoice.", "Compare every order line."],
  }
  vi.mocked(http.put).mockImplementationOnce(async () => {
    item = { ...item, revision: 8, state: "candidate", content: revised, validation: {} }
    return item
  })
  mount()
  await screen.findByRole("heading", { name: procedure.title })
  fireEvent.click(button("experience.edit"))
  fireEvent.change(screen.getByLabelText("experience.field.title"), { target: { value: revised.title } })
  fireEvent.change(screen.getByLabelText("experience.field.steps"), {
    target: { value: revised.steps.join("\n") },
  })
  expect(screen.queryByRole("button", { name: "experience.enable" })).toBeNull()
  fireEvent.click(button("experience.saveDraft"))
  await screen.findByRole("heading", { name: revised.title })
  expect(http.put).toHaveBeenCalledWith(`${endpoint}/experience-1`, {
    expected_revision: 7,
    content: revised,
  })
  expect(screen.getByText(/experience\.state\.candidate/)).toBeTruthy()
  expect(screen.queryByRole("button", { name: "experience.enable" })).toBeNull()
  expect(screen.queryByRole("button", { name: "experience.saveDraft" })).toBeNull()
})

it.each(["review", "edit", "enable"] as const)(
  "shows a backend revision conflict for %s without claiming a successful transition",
  async (action) => {
    if (action === "enable") item = { ...item, state: "validated", validation: { passed: true } }
    const conflict = new ApiError(409, "MEMORY_CONFLICT", "This experience changed; reload its current revision.")
    vi.mocked(http.post).mockRejectedValueOnce(conflict)
    vi.mocked(http.put).mockRejectedValueOnce(conflict)
    mount()
    await screen.findByRole("heading", { name: procedure.title })
    if (action === "review") {
      fillReview()
      fireEvent.click(button("experience.recordReview"))
    } else if (action === "edit") {
      fireEvent.click(button("experience.edit"))
      fireEvent.change(screen.getByLabelText("experience.field.title"), {
        target: { value: "Unsaved local title" },
      })
      fireEvent.click(button("experience.saveDraft"))
    } else {
      fireEvent.click(button("experience.enable"))
    }
    expect((await screen.findByRole("alert")).textContent).toBe("MEMORY_CONFLICT")
    expect(http.get).toHaveBeenCalledTimes(1) // A failed write is not a successful refresh.
    expect(screen.getByText(/experience\.version:7/)).toBeTruthy()
    expect(screen.getByText(new RegExp(`experience\\.state\\.${item.state}`))).toBeTruthy()
    expect(screen.getByRole("heading", { name: procedure.title })).toBeTruthy()
    expect(screen.queryByRole("heading", { name: "Unsaved local title" })).toBeNull()
    expect(screen.queryByRole("button", { name: "experience.retire" })).toBeNull()
    if (action !== "enable") expect(screen.queryByRole("button", { name: "experience.enable" })).toBeNull()
    const write = action === "edit" ? http.put : http.post
    expect(write).toHaveBeenCalledWith(
      action === "edit" ? `${endpoint}/experience-1` : `${endpoint}/experience-1/${action}`,
      expect.objectContaining({ expected_revision: 7 }),
    )
  },
)

it("hides the procedure and enable action when the server reports withdrawn sources", async () => {
  item = { ...item, state: "needs_review", content: null, sources_available: false, validation: {} }
  mount()
  await screen.findByRole("heading", { name: "experience.unavailable" })
  expect(screen.queryByText(procedure.conditions)).toBeNull()
  expect(screen.queryByRole("textbox")).toBeNull()
  for (const name of ["experience.edit", "experience.enable", "experience.recordReview", "experience.saveDraft"])
    expect(screen.queryByRole("button", { name })).toBeNull()
  expect(http.post).not.toHaveBeenCalled()
  expect(http.put).not.toHaveBeenCalled()
})

it.each([false, true])(
  "removes withdrawn content after refetch without requiring a revision bump (editing=%s)",
  async (editing) => {
    item = { ...item, state: "validated", validation: { passed: true, notes: reviewNotes } }
    mount()
    await screen.findByRole("heading", { name: procedure.title })
    if (editing) fireEvent.click(button("experience.edit"))
    // Source revocation changes the read view, not the experience revision.
    // This is the backend's actual view() shape, including cleared validation.
    item = { ...item, state: "needs_review", content: null, sources_available: false, validation: {} }
    await act(async () => {
      await client.invalidateQueries()
    })
    await screen.findByRole("heading", { name: "experience.unavailable" })
    expect(screen.queryByRole("heading", { name: procedure.title })).toBeNull()
    expect(screen.queryByText(procedure.conditions)).toBeNull()
    expect(screen.queryByRole("textbox")).toBeNull()
    expect(screen.queryByDisplayValue(procedure.title)).toBeNull()
    expect(screen.queryByText(reviewNotes)).toBeNull()
    for (const name of ["experience.enable", "experience.saveDraft", "experience.edit", "experience.recordReview"])
      expect(screen.queryByRole("button", { name })).toBeNull()
    expect(http.post).not.toHaveBeenCalled()
    expect(http.put).not.toHaveBeenCalled()
  },
)
