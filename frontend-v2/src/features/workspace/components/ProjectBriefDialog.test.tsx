import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { ApiError, http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import type { Project } from "@/shared/types/api"
import type { ProjectBrief } from "../api/brief"
import { ProjectBriefDialog } from "./ProjectBriefDialog"

vi.mock("@/shared/api/http", async (original) => ({
  ...await original<typeof import("@/shared/api/http")>(), http: { get: vi.fn(), put: vi.fn() },
}))
vi.mock("react-i18next", async (original) => ({ ...await original<typeof import("react-i18next")>(), useTranslation: () => ({
  t: (key: string, values?: Record<string, unknown>) => {
    const shown = Object.entries(values ?? {}).filter(([name]) => name !== "ns").map(([, value]) => String(value))
    return shown.length ? `${key}:${shown.join("/")}` : key
  },
}) }))
vi.mock("@/shared/hooks/useApiErrorMessage", () => ({ useApiErrorMessage: () => () => "Request failed" }))

const project = { id: "p1", name: "Snake" } as Project
const recently = new Date(Date.now() - 60_000).toISOString()
const view = (extra: Partial<ProjectBrief> = {}): ProjectBrief => ({ id: "brief-1", project_id: "p1", content: "Goal: ship v1",
  revision: 3, updated_by: "assistant", created_at: recently, updated_at: recently, ...extra })
const none: ProjectBrief = { id: null, project_id: "p1", content: "", revision: 0, updated_by: null, updated_at: null }
let client: QueryClient
beforeEach(() => {
  vi.clearAllMocks()
  useAuthStore.setState({ user: { id: "owner" } as never })
  useWorkspaceStore.setState({ currentId: "workspace" })
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
})
afterEach(() => { cleanup(); client.clear() })
function mount() {
  const onClose = vi.fn()
  render(<QueryClientProvider client={client}><ProjectBriefDialog project={project} onClose={onClose} /></QueryClientProvider>)
  return onClose
}
const editor = () => screen.findByRole("textbox", { name: "brief.label" }) as Promise<HTMLTextAreaElement>
const saveButton = () => screen.getByRole("button", { name: "common:action.save" }) as HTMLButtonElement

describe("project brief", () => {
  it("loads the brief with who last updated it", async () => {
    vi.mocked(http.get).mockResolvedValue(view())
    mount()
    expect((await editor()).value).toBe("Goal: ship v1")
    expect(http.get).toHaveBeenCalledWith("/api/projects/p1/brief", expect.objectContaining({ signal: expect.any(AbortSignal) }))
    expect(screen.getByText("Snake")).toBeTruthy()
    expect(screen.getByText(/brief\.byAssistant/)).toBeTruthy()
    expect(screen.queryByText("brief.empty")).toBeNull()
    expect(screen.getByText("brief.count:13/6000")).toBeTruthy()
    expect(saveButton().disabled).toBe(true)
  })

  it.each([["an empty view", none], ["null from an older server", null]])("offers %s to write and creates it at revision 0", async (_label, empty) => {
    vi.mocked(http.get).mockResolvedValue(empty)
    vi.mocked(http.put).mockResolvedValue(view({ content: "Goal: a snake game", revision: 1, updated_by: "user" }))
    mount()
    const text = await editor()
    expect(screen.getByText("brief.empty")).toBeTruthy()
    expect(screen.queryByText(/brief\.by/)).toBeNull()
    fireEvent.change(text, { target: { value: "Goal: a snake game" } })
    fireEvent.click(saveButton())
    await screen.findByText("brief.saved")
    expect(http.put).toHaveBeenCalledExactlyOnceWith("/api/projects/p1/brief", { content: "Goal: a snake game", expected_revision: 0 })
    expect(screen.getByText(/brief\.byUser/)).toBeTruthy()
    expect(screen.queryByText("brief.empty")).toBeNull()
    expect(saveButton().disabled).toBe(true)
  })

  it("saves an edit against the revision it was based on", async () => {
    vi.mocked(http.get).mockResolvedValue(view())
    vi.mocked(http.put).mockResolvedValue(view({ content: "Goal: ship v2", revision: 4, updated_by: "user" }))
    mount()
    fireEvent.change(await editor(), { target: { value: "Goal: ship v2" } })
    fireEvent.click(saveButton())
    await screen.findByText("brief.saved")
    expect(http.put).toHaveBeenCalledWith("/api/projects/p1/brief", { content: "Goal: ship v2", expected_revision: 3 })
  })

  it("reloads after a conflict, keeps the person's text and saves it on the new revision only when asked", async () => {
    vi.mocked(http.get).mockResolvedValueOnce(view())
      .mockResolvedValueOnce(view({ content: "Progress: level 2 done", revision: 4, updated_by: "assistant" }))
    vi.mocked(http.put)
      .mockRejectedValueOnce(new ApiError(409, "PROJECT_BRIEF_REVISION_CONFLICT", "changed", { current_revision: 4 }))
      .mockResolvedValueOnce(view({ content: "Goal: ship v2", revision: 5, updated_by: "user" }))
    mount()
    const text = await editor()
    fireEvent.change(text, { target: { value: "Goal: ship v2" } })
    fireEvent.click(saveButton())
    expect((await screen.findByRole("alert")).textContent).toContain("brief.conflict")
    expect(http.get).toHaveBeenCalledTimes(2)
    expect(text.value).toBe("Goal: ship v2")
    expect(http.put).toHaveBeenCalledTimes(1)
    fireEvent.click(saveButton())
    await screen.findByText("brief.saved")
    expect(vi.mocked(http.put).mock.calls[1]).toEqual(["/api/projects/p1/brief", { content: "Goal: ship v2", expected_revision: 4 }])
  })

  it("can drop the person's text for the version that won the conflict", async () => {
    vi.mocked(http.get).mockResolvedValueOnce(view())
      .mockResolvedValueOnce(view({ content: "Progress: level 2 done", revision: 4, updated_by: "assistant" }))
    vi.mocked(http.put).mockRejectedValueOnce(new ApiError(409, "PROJECT_BRIEF_REVISION_CONFLICT", "changed", { current_revision: 4 }))
    mount()
    const text = await editor()
    fireEvent.change(text, { target: { value: "Goal: ship v2" } })
    fireEvent.click(saveButton())
    fireEvent.click(await screen.findByRole("button", { name: "brief.showLatest" }))
    expect(text.value).toBe("Progress: level 2 done")
    expect(screen.queryByRole("alert")).toBeNull()
    expect(saveButton().disabled).toBe(true)
  })

  it.each([
    [new ApiError(422, "PROJECT_BRIEF_TOO_LONG", "too long", { max_chars: 6000 }), "brief.tooLong:6000"],
    [new ApiError(422, "PROJECT_BRIEF_SENSITIVE_CONTENT", "secret", { kind: "credential" }), "brief.sensitive"],
    [new ApiError(404, "PROJECT_NOT_FOUND", "missing"), "brief.notFound"],
    [new TypeError("offline"), "Request failed"],
  ])("explains a refused save (%s) and keeps the text", async (error, message) => {
    vi.mocked(http.get).mockResolvedValue(view())
    vi.mocked(http.put).mockRejectedValueOnce(error)
    mount()
    const text = await editor()
    fireEvent.change(text, { target: { value: "API key: sk-123" } })
    fireEvent.click(saveButton())
    expect((await screen.findByRole("alert")).textContent).toBe(message)
    expect(text.value).toBe("API key: sk-123")
    expect(saveButton().disabled).toBe(false)
  })

  it("counts characters as the server does and blocks an over-long save", async () => {
    vi.mocked(http.get).mockResolvedValue(none)
    mount()
    const text = await editor()
    fireEvent.change(text, { target: { value: "🐍" } })
    expect(screen.getByText("brief.count:1/6000")).toBeTruthy()
    fireEvent.change(text, { target: { value: "x".repeat(6001) } })
    expect(screen.getByText("brief.count:6001/6000").className).toContain("text-dangerink")
    expect(saveButton().disabled).toBe(true)
    fireEvent.click(saveButton())
    expect(http.put).not.toHaveBeenCalled()
  })

  it("explains a project that cannot be read and retries on request", async () => {
    vi.mocked(http.get).mockRejectedValueOnce(new ApiError(404, "PROJECT_NOT_FOUND", "missing")).mockResolvedValueOnce(view())
    mount()
    expect((await screen.findByRole("alert")).textContent).toContain("brief.notFound")
    expect(screen.queryByRole("textbox")).toBeNull()
    fireEvent.click(screen.getByRole("button", { name: "common:action.retry" }))
    expect((await editor()).value).toBe("Goal: ship v1")
  })

  it("closes on request", async () => {
    vi.mocked(http.get).mockResolvedValue(view())
    const onClose = mount()
    await editor()
    fireEvent.click(screen.getByRole("button", { name: "common:action.close" }))
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce())
  })
})
