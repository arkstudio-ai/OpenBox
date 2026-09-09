import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { ApiError, http } from "@/shared/api/http"
import type { QuestionRequest } from "@/shared/types/api"
import { usePendingStore } from "../stores/pending"
import { questionDraftKey } from "../api/question"
import { QuestionDock } from "./QuestionDock"

vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
vi.mock("../api/messages", () => ({ useUserId: () => "u1" }))

const request: QuestionRequest = {
  id: "q1",
  session_id: "s1",
  status: "pending",
  generation: 1,
  draft_revision: 0,
  questions: [
    { question: "Duration?", options: [{ label: "30s" }, { label: "60s" }] },
    { question: "Extras?", multiple: true, options: [{ label: "Captions" }, { label: "Music" }] },
    { question: "Format?", options: [{ label: "Portrait" }, { label: "Landscape" }] },
  ],
}

function mount(value = request) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const rendered = render(
    <QueryClientProvider client={client}>
      <QuestionDock request={value} />
    </QueryClientProvider>,
  )
  return { ...rendered, client }
}

function answerAll() {
  visitPage(0)
  fireEvent.click(screen.getByRole("button", { name: "30s" }))
  fireEvent.click(screen.getByRole("button", { name: "Captions" }))
  fireEvent.click(screen.getByRole("button", { name: "Music" }))
  fireEvent.click(screen.getByRole("button", { name: "question.next" }))
  fireEvent.change(screen.getByRole("textbox", { name: "Format?" }), { target: { value: "Square" } })
}

function visitPage(page: number) {
  fireEvent.click(screen.getByRole("button", { name: "question.previous" }))
  fireEvent.click(screen.getByRole("button", { name: "question.previous" }))
  for (let i = 0; i < page; i += 1) fireEvent.click(screen.getByRole("button", { name: "question.next" }))
  expect(screen.getByText(`${page + 1}/3`)).toBeTruthy()
}

beforeEach(() => {
  localStorage.clear()
  usePendingStore.setState({ questions: new Map([["s1", [request]]]), closedQuestions: new Set() })
  vi.spyOn(http, "post").mockResolvedValue({ ok: true })
  vi.spyOn(http, "put").mockImplementation(async (_path, body) => {
    const data = body as { draft: QuestionRequest["draft"]; revision: number }
    return { ...request, draft: data.draft, draft_revision: data.revision + 1 } as never
  })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  localStorage.clear()
})

describe("one durable ask with multiple questions", () => {
  it("refreshes a clean mounted draft from the server but preserves dirty local edits", () => {
    const { rerender, client } = mount()
    const remote = {
      ...request,
      draft_revision: 1,
      draft: request.questions.map(() => ({ selected: [], custom: "From mobile", use_custom: true })),
    }
    rerender(
      <QueryClientProvider client={client}>
        <QuestionDock request={remote} />
      </QueryClientProvider>,
    )
    visitPage(2)
    expect((screen.getByRole("textbox", { name: "Format?" }) as HTMLInputElement).value).toBe("From mobile")
    fireEvent.change(screen.getByRole("textbox", { name: "Format?" }), { target: { value: "Unsaved local" } })
    rerender(
      <QueryClientProvider client={client}>
        <QuestionDock
          request={{
            ...remote,
            draft_revision: 2,
            draft: remote.draft.map((d) => ({ ...d, custom: "Newer remote" })),
          }}
        />
      </QueryClientProvider>,
    )
    expect((screen.getByRole("textbox", { name: "Format?" }) as HTMLInputElement).value).toBe("Unsaved local")
  })

  it("disables all answer controls while submitting", async () => {
    let finish!: () => void
    vi.mocked(http.post).mockReturnValue(
      new Promise((resolve) => {
        finish = () => resolve({ ok: true, session_id: "s1" })
      }) as never,
    )
    mount()
    answerAll()
    fireEvent.click(screen.getByRole("button", { name: "question.submit" }))
    await waitFor(() => expect(screen.getByRole("button", { name: "question.submitting" })).toBeTruthy())
    expect((screen.getByRole("button", { name: "question.submitting" }) as HTMLButtonElement).disabled).toBe(
      true,
    )
    expect(screen.getByRole("textbox", { name: "Format?" }).closest("fieldset")?.disabled).toBe(true)
    expect((screen.getByRole("button", { name: "question.previous" }) as HTMLButtonElement).disabled).toBe(
      true,
    )
    expect((screen.getByRole("button", { name: "question.next" }) as HTMLButtonElement).disabled).toBe(true)
    finish()
    await waitFor(() => expect(usePendingStore.getState().questions.get("s1")).toEqual([]))
  })
  it("shows only one page, its position and one shared submit; nothing is preselected", () => {
    mount()
    expect(screen.getByText("1/3")).toBeTruthy()
    expect(screen.queryByText("2/3")).toBeNull()
    expect(screen.queryByText("3/3")).toBeNull()
    expect(screen.queryByRole("textbox", { name: "Extras?" })).toBeNull()
    expect(screen.queryByRole("textbox", { name: "Format?" })).toBeNull()
    expect(screen.getAllByRole("button", { name: "question.submit" })).toHaveLength(1)
    expect((screen.getByRole("button", { name: "question.submit" }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByRole("button", { name: "30s" }).getAttribute("aria-pressed")).toBe("false")
  })

  it("sends one ordered nested answer payload only after every question is answered", async () => {
    mount()
    fireEvent.click(screen.getByRole("button", { name: "30s" }))
    expect((screen.getByRole("button", { name: "question.submit" }) as HTMLButtonElement).disabled).toBe(true)
    expect(http.post).not.toHaveBeenCalled()
    answerAll()
    fireEvent.click(screen.getByRole("button", { name: "question.submit" }))
    await waitFor(() =>
      expect(http.post).toHaveBeenCalledWith("/api/agent/question/q1", {
        answers: [["30s"], ["Captions", "Music"], ["Square"]],
      }),
    )
    await waitFor(() => expect(usePendingStore.getState().questions.get("s1")).toEqual([]))
    expect(localStorage.getItem(`${questionDraftKey("u1", "q1")}:page`)).toBeNull()
  })

  it("keeps choices and free text across a reload, even before autosave succeeds", () => {
    const first = mount()
    answerAll()
    first.unmount()
    mount()
    expect(screen.getByText("3/3")).toBeTruthy()
    expect((screen.getByRole("textbox", { name: "Format?" }) as HTMLInputElement).value).toBe("Square")
    visitPage(0)
    expect(screen.getByRole("button", { name: "30s" }).getAttribute("aria-pressed")).toBe("true")
    visitPage(1)
    expect(screen.getByRole("button", { name: "Captions" }).getAttribute("aria-pressed")).toBe("true")
    visitPage(2)
    expect((screen.getByRole("textbox", { name: "Format?" }) as HTMLInputElement).value).toBe("Square")
    expect((screen.getByRole("button", { name: "question.submit" }) as HTMLButtonElement).disabled).toBe(
      false,
    )
  })

  it("loads durable drafts from the server and does not borrow another user's local draft", () => {
    localStorage.setItem(
      questionDraftKey("other-user", "q1"),
      JSON.stringify({
        revision: 2,
        draft: request.questions.map(() => ({ selected: [], custom: "private", use_custom: true })),
      }),
    )
    mount({
      ...request,
      draft_revision: 2,
      draft: [
        { selected: ["60s"], custom: "", use_custom: false },
        { selected: ["Music"], custom: "", use_custom: false },
        { selected: [], custom: "Server draft", use_custom: true },
      ],
    })
    expect((screen.getByRole("textbox", { name: "Format?" }) as HTMLInputElement).value).toBe("Server draft")
    visitPage(0)
    expect(screen.getByRole("button", { name: "60s" }).getAttribute("aria-pressed")).toBe("true")
    visitPage(2)
    expect((screen.getByRole("textbox", { name: "Format?" }) as HTMLInputElement).value).toBe("Server draft")
  })

  it("autosaves all draft fields with a revision, without submitting the answer", async () => {
    mount()
    answerAll()
    await waitFor(() =>
      expect(http.put).toHaveBeenCalledWith("/api/agent/question/q1/draft", {
        revision: 0,
        draft: [
          { selected: ["30s"], custom: "", use_custom: false },
          { selected: ["Captions", "Music"], custom: "", use_custom: false },
          { selected: [], custom: "Square", use_custom: true },
        ],
      }),
    )
    expect(http.post).not.toHaveBeenCalled()
  })

  it("preserves local edits on draft conflict and retries only with a fresh server revision", async () => {
    vi.mocked(http.put).mockRejectedValueOnce(new ApiError(409, "QUESTION_CONFLICT", "changed elsewhere"))
    vi.spyOn(http, "get").mockResolvedValue({ ...request, draft_revision: 8 } as never)
    mount()
    answerAll()
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("question.draftConflict"))
    expect((screen.getByRole("textbox", { name: "Format?" }) as HTMLInputElement).value).toBe("Square")
    fireEvent.click(screen.getByRole("button", { name: "question.retrySave" }))
    await waitFor(() =>
      expect(http.put).toHaveBeenLastCalledWith(
        "/api/agent/question/q1/draft",
        expect.objectContaining({ revision: 8 }),
      ),
    )
    expect(http.post).not.toHaveBeenCalled()
  })

  it("shows a failed submission, retains input, and lets the user retry", async () => {
    vi.mocked(http.post).mockRejectedValueOnce(new ApiError(503, "UNAVAILABLE", "offline"))
    mount()
    answerAll()
    fireEvent.click(screen.getByRole("button", { name: "question.submit" }))
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("question.submitFailed"))
    expect((screen.getByRole("textbox", { name: "Format?" }) as HTMLInputElement).value).toBe("Square")
    expect(usePendingStore.getState().questions.get("s1")).toHaveLength(1)
    fireEvent.click(screen.getByRole("button", { name: "question.submit" }))
    await waitFor(() => expect(http.post).toHaveBeenCalledTimes(2))
  })

  it("removes an expired ask and cannot resurrect it from a delayed event", async () => {
    vi.mocked(http.post).mockRejectedValueOnce(new ApiError(410, "QUESTION_GONE", "expired"))
    mount()
    answerAll()
    fireEvent.click(screen.getByRole("button", { name: "question.submit" }))
    await waitFor(() => expect(usePendingStore.getState().closedQuestions.has("q1")).toBe(true))
    usePendingStore.getState().addQuestion(request)
    usePendingStore.getState().setQuestions([request])
    expect(usePendingStore.getState().questions.get("s1") ?? []).toEqual([])
  })

  it("skips the entire group, rather than submitting a partly answered group", async () => {
    mount()
    fireEvent.click(screen.getByRole("button", { name: "question.skipAll" }))
    await waitFor(() => expect(http.post).toHaveBeenCalledWith("/api/agent/question/q1/reject"))
  })

  it("advances a single selection, leaves multiselect open, and preserves edits when going back", () => {
    mount()
    fireEvent.click(screen.getByRole("button", { name: "30s" }))
    expect(screen.getByText("2/3")).toBeTruthy()
    expect(screen.queryByRole("button", { name: "30s" })).toBeNull()
    fireEvent.click(screen.getByRole("button", { name: "Captions" }))
    expect(screen.getByText("2/3")).toBeTruthy()
    fireEvent.click(screen.getByRole("button", { name: "Music" }))
    visitPage(0)
    expect(screen.getByRole("button", { name: "30s" }).getAttribute("aria-pressed")).toBe("true")
    fireEvent.click(screen.getByRole("button", { name: "60s" }))
    expect(screen.getByRole("button", { name: "Music" }).getAttribute("aria-pressed")).toBe("true")
    expect(http.post).not.toHaveBeenCalled()
  })

  it("commits custom input with Enter but never advances on typing or IME confirmation", () => {
    mount()
    const input = screen.getByRole("textbox", { name: "Duration?" })
    fireEvent.change(input, { target: { value: " " } })
    fireEvent.keyDown(input, { key: "Enter" })
    expect(screen.getByText("1/3")).toBeTruthy()
    fireEvent.change(input, { target: { value: "四十五秒" } })
    expect(screen.getByText("1/3")).toBeTruthy()
    fireEvent.keyDown(input, { key: "Enter", isComposing: true })
    fireEvent.keyDown(input, { key: "Enter", keyCode: 229 })
    expect(screen.getByText("1/3")).toBeTruthy()
    fireEvent.keyDown(input, { key: "Enter" })
    expect(screen.getByText("2/3")).toBeTruthy()
    visitPage(0)
    expect((screen.getByRole("textbox", { name: "Duration?" }) as HTMLInputElement).value).toBe("四十五秒")
    expect(http.post).not.toHaveBeenCalled()
  })

  it("allows manual navigation past unanswered pages without accepting a partial answer", () => {
    mount()
    visitPage(2)
    fireEvent.click(screen.getByRole("button", { name: "Portrait" }))
    expect(screen.getByText("3/3")).toBeTruthy()
    expect((screen.getByRole("button", { name: "question.submit" }) as HTMLButtonElement).disabled).toBe(true)
    expect(http.post).not.toHaveBeenCalled()
    visitPage(0)
  })

  it("does not submit automatically after the final custom input is completed", () => {
    mount()
    answerAll()
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Format?" }), { key: "Enter" })
    expect(screen.getByText("3/3")).toBeTruthy()
    expect((screen.getByRole("button", { name: "question.submit" }) as HTMLButtonElement).disabled).toBe(
      false,
    )
    expect(http.post).not.toHaveBeenCalled()
  })

  it("restores the first unanswered page if no local page preference exists", () => {
    mount({ ...request, draft: [{ selected: ["30s"], custom: "", use_custom: false }] })
    expect(screen.getByText("2/3")).toBeTruthy()
  })

  it.each(["broken", "-1", "9", "1.5"])("ignores invalid saved page %s", (page) => {
    localStorage.setItem(`${questionDraftKey("u1", "q1")}:page`, page)
    mount()
    expect(screen.getByText("1/3")).toBeTruthy()
  })

  it("page navigation alone does not save an answer or increment the draft revision", async () => {
    mount()
    visitPage(1)
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 450))
    })
    expect(http.put).not.toHaveBeenCalled()
    expect(http.post).not.toHaveBeenCalled()
  })

  it("keeps one-question cards usable without unnecessary pager buttons", () => {
    mount({ ...request, questions: [request.questions[0]] })
    expect(screen.getByText("1/1")).toBeTruthy()
    expect(screen.queryByRole("button", { name: "question.next" })).toBeNull()
    fireEvent.click(screen.getByRole("button", { name: "30s" }))
    expect((screen.getByRole("button", { name: "question.submit" }) as HTMLButtonElement).disabled).toBe(
      false,
    )
    expect(http.post).not.toHaveBeenCalled()
  })

  it.each([2, 4])("supports %s pages without advancing beyond the last page", async (count) => {
    mount({
      ...request,
      questions: Array.from({ length: count }, (_, i) => ({
        question: `Question ${i}`,
        custom: false,
        options: [{ label: `Answer ${i}` }],
      })),
    })
    for (let i = 0; i < count; i += 1) {
      expect(screen.getByText(`${i + 1}/${count}`)).toBeTruthy()
      fireEvent.click(screen.getByRole("button", { name: `Answer ${i}` }))
    }
    expect(screen.getByText(`${count}/${count}`)).toBeTruthy()
    expect(http.post).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole("button", { name: "question.submit" }))
    await waitFor(() =>
      expect(http.post).toHaveBeenCalledWith("/api/agent/question/q1", {
        answers: Array.from({ length: count }, (_, i) => [`Answer ${i}`]),
      }),
    )
  })
})
