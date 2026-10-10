import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import type { QuestionRequest } from "@/shared/types/api"
import type { MentionScope } from "../hooks/useMentionMenu"
import { completeAsset, createAsset, putToOss } from "../api/assets"
import { usePendingStore } from "../stores/pending"
import { QuestionDock } from "./QuestionDock"

vi.mock("react-i18next", async (importOriginal) => ({
  ...await importOriginal<typeof import("react-i18next")>(),
  useTranslation: () => ({ t: (key: string) => key }),
}))
vi.mock("../api/messages", () => ({ useUserId: () => "u1" }))
vi.mock("../api/assets", () => ({ createAsset: vi.fn(), completeAsset: vi.fn(), putToOss: vi.fn() }))

const request: QuestionRequest = {
  id: "assets-question", session_id: "s1", draft_revision: 0,
  questions: [{ question: "Provide a presenter picture", allow_attachments: true }],
}
const scope: MentionScope = {
  projects: [], project: "all", setProject: vi.fn(), source: "all", setSource: vi.fn(), loading: false,
  items: [{ id: "asset-a", name: "portrait.png", mime: "image/png", size: 100,
    kind: "image", sandboxPath: "/workspace/uploads/portrait.png", url: "https://assets.example/portrait.png" }],
}

function mount(value = request) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><QuestionDock request={value} resourceScope={scope} /></QueryClientProvider>)
}

function pickResource() {
  fireEvent.click(screen.getByRole("button", { name: "composer.resourceCenter" }))
  fireEvent.click(screen.getByRole("button", { name: /portrait.png/ }))
}

beforeEach(() => {
  localStorage.clear()
  usePendingStore.setState({ questions: new Map([["s1", [request]]]), closedQuestions: new Set() })
  vi.spyOn(http, "post").mockResolvedValue({ ok: true, session_id: "s1" })
  vi.spyOn(http, "get").mockResolvedValue({ name: "portrait.png", mime: "image/png", size: 100, url: "https://assets.example/portrait.png" } as never)
  vi.spyOn(http, "put").mockImplementation(async (_path, body) => {
    const data = body as { draft: QuestionRequest["draft"]; revision: number }
    return { ...request, draft: data.draft, draft_revision: data.revision + 1 } as never
  })
  vi.stubGlobal("URL", { createObjectURL: vi.fn(() => "blob:test"), revokeObjectURL: vi.fn() })
  vi.mocked(createAsset).mockResolvedValue({ id: "asset-a", name: "portrait.png", sandboxPath: "/workspace/portrait.png", putUrl: "https://assets.example/put", headers: {} })
  vi.mocked(completeAsset).mockResolvedValue({ id: "asset-a", name: "portrait.png", sandboxPath: "/workspace/portrait.png", mime: "image/png", size: 100, url: "https://assets.example/portrait.png" })
  vi.mocked(putToOss).mockResolvedValue(undefined)
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
  localStorage.clear()
})

describe("Ask resource answers", () => {
  it("offers resource actions only on questions that request attachments", () => {
    mount({ ...request, questions: [{ question: "How long?" }] })
    expect(screen.queryByRole("button", { name: "composer.uploadFile" })).toBeNull()
  })

  it("submits a resource-only answer without uploading a library asset again", async () => {
    mount()
    pickResource()
    fireEvent.click(screen.getByTestId("question-primary-action"))
    await waitFor(() => expect(http.post).toHaveBeenCalledWith("/api/agent/question/assets-question", {
      answers: [[]], attachments: [["asset-a"]],
    }))
    expect(createAsset).not.toHaveBeenCalled()
    expect(putToOss).not.toHaveBeenCalled()
  })

  it("preserves attachment drafts across refresh and does not submit while autosaving", async () => {
    const first = mount()
    pickResource()
    await waitFor(() => expect(http.put).toHaveBeenCalledWith("/api/agent/question/assets-question/draft", {
      revision: 0, draft: [{ selected: [], custom: "", use_custom: false, attachments: ["asset-a"] }],
    }))
    first.unmount()
    // Use the same revision returned by the server's durable draft.
    mount({ ...request, draft_revision: 1, draft: [{ selected: [], custom: "", use_custom: false, attachments: ["asset-a"] }] })
    await waitFor(() => expect(screen.getByText("portrait.png")).toBeTruthy())
    expect((screen.getByTestId("question-primary-action") as HTMLButtonElement).disabled).toBe(false)
    expect(http.post).not.toHaveBeenCalled()
  })

  it("waits for OSS completion before an uploaded file can be submitted", async () => {
    let finish!: () => void
    vi.mocked(putToOss).mockImplementation(() => new Promise<void>((resolve) => { finish = resolve }))
    mount()
    fireEvent.change(screen.getByLabelText("question.uploadResources"), {
      target: { files: [new File(["image"], "portrait.png", { type: "image/png" })] },
    })
    await waitFor(() => expect(putToOss).toHaveBeenCalledTimes(1))
    expect((screen.getByTestId("question-primary-action") as HTMLButtonElement).disabled).toBe(true)
    expect(completeAsset).not.toHaveBeenCalled()
    await act(async () => finish())
    await waitFor(() => expect((screen.getByTestId("question-primary-action") as HTMLButtonElement).disabled).toBe(false))
    expect(createAsset).toHaveBeenCalledWith("portrait.png", "image/png", 5, "s1")
    fireEvent.click(screen.getByTestId("question-primary-action"))
    await waitFor(() => expect(http.post).toHaveBeenCalledWith("/api/agent/question/assets-question", {
      answers: [[]], attachments: [["asset-a"]],
    }))
  })

  it("blocks a failed upload even if a text answer is present", async () => {
    vi.mocked(putToOss).mockRejectedValueOnce(new Error("network failed"))
    mount()
    fireEvent.change(screen.getByRole("textbox", { name: "Provide a presenter picture" }), { target: { value: "Use this" } })
    fireEvent.change(screen.getByLabelText("question.uploadResources"), { target: { files: [new File(["image"], "portrait.png", { type: "image/png" })] } })
    await waitFor(() => expect(screen.getByText("question.resourcesFailed")).toBeTruthy())
    expect((screen.getByTestId("question-primary-action") as HTMLButtonElement).disabled).toBe(true)
    expect(http.post).not.toHaveBeenCalled()
  })

  it("keeps files paired with their question and leaves room to attach after selecting an option", async () => {
    mount({ ...request, questions: [
      { question: "Provide a presenter picture", allow_attachments: true, options: [{ label: "Use existing" }] },
      { question: "Duration?", options: [{ label: "30s" }] },
    ] })
    fireEvent.click(screen.getByRole("button", { name: "Use existing" }))
    expect(screen.getByText("1/2")).toBeTruthy()
    pickResource()
    fireEvent.click(screen.getByTestId("question-primary-action"))
    fireEvent.click(screen.getByRole("button", { name: "30s" }))
    fireEvent.click(screen.getByTestId("question-primary-action"))
    await waitFor(() => expect(http.post).toHaveBeenCalledWith("/api/agent/question/assets-question", {
      answers: [["Use existing"], ["30s"]], attachments: [["asset-a"], []],
    }))
  })

  it("deselects a resource without deleting it from the library", async () => {
    mount()
    pickResource()
    fireEvent.click(screen.getByRole("button", { name: /portrait.png/ }))
    expect((screen.getByTestId("question-primary-action") as HTMLButtonElement).disabled).toBe(true)
    expect(http.post).not.toHaveBeenCalled()
  })
})
