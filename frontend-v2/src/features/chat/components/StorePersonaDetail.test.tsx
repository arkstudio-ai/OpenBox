// The persona card inside the dock: one field per memory, no free-text box,
// and the answer composed from the pill plus whatever was edited.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import type { QuestionRequest, ToolPart } from "@/shared/types/api"
import { usePendingStore } from "../stores/pending"
import { QuestionDock } from "./QuestionDock"
import { StorePersonaDetail } from "./StorePersonaDetail"
import { QuestionAnswered } from "./tool/QuestionAnswered"

vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
vi.mock("../api/messages", () => ({ useUserId: () => "u1" }))

const request: QuestionRequest = {
  id: "q1",
  session_id: "s1",
  status: "pending",
  generation: 1,
  draft_revision: 0,
  questions: [
    {
      question: "这是我对你的店的理解，看看对不对？",
      header: "店铺人设确认",
      custom: true,
      options: [{ label: "确认" }, { label: "稍后" }],
      detail: {
        kind: "store_persona_bundle",
        items: [
          { memory_id: "m1", type: "IDENTITY", label: "店是谁", summary: "南宁·泽岚鲜果" },
          { memory_id: "m2", type: "VOICE", label: "表达风格", summary: "亲切、直接" },
        ],
      },
    },
  ],
}

function mount(value = request) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <QuestionDock request={value} />
    </QueryClientProvider>,
  )
}

// An edited field's name also carries its "edited" badge, hence the prefix match.
const field = (label: string) =>
  screen.getByRole("textbox", { name: new RegExp(`^${label}`) }) as HTMLTextAreaElement

beforeEach(() => {
  localStorage.clear()
  usePendingStore.setState({ questions: new Map([["s1", [request]]]), closedQuestions: new Set() })
  vi.spyOn(http, "post").mockResolvedValue({ ok: true, session_id: "s1" })
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

describe("StorePersonaDetail", () => {
  it("renders nothing for a question that is not a persona bundle", () => {
    const { container } = render(
      <StorePersonaDetail
        item={{ question: "x", detail: { kind: "desktop_takeover" } }}
        draft={{ selected: [], custom: "", use_custom: false }}
        onChange={() => undefined}
      />,
    )
    expect(container.innerHTML).toBe("")
  })

  it("shows one field per memory, seeded with its summary, and no free-text box", () => {
    mount()
    expect(field("店是谁").value).toBe("南宁·泽岚鲜果")
    expect(field("表达风格").value).toBe("亲切、直接")
    expect(screen.getAllByRole("textbox")).toHaveLength(2)
    expect(screen.queryByPlaceholderText("question.other")).toBeNull()
  })

  it("submits the plain 确认 when nothing was edited", async () => {
    mount()
    fireEvent.click(screen.getByRole("button", { name: "确认" }))
    fireEvent.click(screen.getByRole("button", { name: "question.submit" }))
    await waitFor(() => expect(http.post).toHaveBeenCalledWith("/api/agent/question/q1", { answers: [["确认"]] }))
  })

  it("submits 确认 with the edited fields as the structured answer", async () => {
    mount()
    fireEvent.change(field("店是谁"), { target: { value: "南宁·泽岚鲜果，人均 25" } })
    expect(screen.getByText("question.persona.edited")).toBeTruthy()
    fireEvent.click(screen.getByRole("button", { name: "确认" }))
    fireEvent.click(screen.getByRole("button", { name: "question.submit" }))
    await waitFor(() =>
      expect(http.post).toHaveBeenCalledWith("/api/agent/question/q1", {
        answers: [[JSON.stringify({ confirm: true, items: { m1: "南宁·泽岚鲜果，人均 25" } })]],
      }),
    )
  })

  it("submits 稍后 as itself even after edits", async () => {
    mount()
    fireEvent.change(field("表达风格"), { target: { value: "活泼" } })
    fireEvent.click(screen.getByRole("button", { name: "稍后" }))
    fireEvent.click(screen.getByRole("button", { name: "question.submit" }))
    await waitFor(() => expect(http.post).toHaveBeenCalledWith("/api/agent/question/q1", { answers: [["稍后"]] }))
  })

  it("keeps the edits in the autosaved draft without making it a custom answer", async () => {
    mount()
    fireEvent.change(field("店是谁"), { target: { value: "改过" } })
    await waitFor(() =>
      expect(http.put).toHaveBeenCalledWith("/api/agent/question/q1/draft", {
        revision: 0,
        draft: [{ selected: [], custom: JSON.stringify({ items: { m1: "改过" } }), use_custom: false }],
      }),
    )
    expect((screen.getByRole("button", { name: "question.submit" }) as HTMLButtonElement).disabled).toBe(true)
  })

  it("restores edited fields from a saved draft", () => {
    mount({
      ...request,
      draft_revision: 1,
      draft: [{ selected: ["确认"], custom: JSON.stringify({ items: { m2: "从手机改的" } }), use_custom: false }],
    })
    expect(field("表达风格").value).toBe("从手机改的")
    expect(field("店是谁").value).toBe("南宁·泽岚鲜果")
    expect(screen.getByRole("button", { name: "确认" }).getAttribute("aria-pressed")).toBe("true")
  })
})

describe("the record of a persona answer", () => {
  const part = (answer: string): ToolPart => ({
    type: "tool",
    id: "t1",
    tool: "question",
    status: "completed",
    metadata: { questions: ["这是我对你的店的理解"], answers: [[answer]] },
  })

  it("reads 已确认（有修改） instead of the structured answer", () => {
    render(<QuestionAnswered part={part(JSON.stringify({ confirm: true, items: { m1: "A" } }))} />)
    expect(screen.getByText("question.persona.confirmedEdited")).toBeTruthy()
    expect(screen.queryByText(/items/)).toBeNull()
  })

  it("shows a plain pill answer as it was", () => {
    render(<QuestionAnswered part={part("确认")} />)
    expect(screen.getByText("确认")).toBeTruthy()
  })
})
