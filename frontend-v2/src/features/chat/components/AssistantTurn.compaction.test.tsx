import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, render, screen, within } from "@testing-library/react"
import type { MessageWithParts } from "@/shared/types/api"
import { mergeTurns } from "../lib/turn-view"
import { AssistantTurn } from "./AssistantTurn"

vi.mock("react-i18next", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-i18next")>()),
  useTranslation: () => ({ t: (key: string) => key }),
}))
vi.mock("./Markdown", () => ({ default: ({ text }: { text: string }) => <p>{text}</p> }))
vi.mock("./meta/AssistantMeta", () => ({
  AssistantMeta: ({ messageId, content }: { messageId: string; content: string }) =>
    <div data-testid="reply-meta" data-message={messageId}>{content}</div>,
}))

afterEach(cleanup)

const request: MessageWithParts = {
  id: "compact", role: "user", agent: "compaction", session_id: "s", created_at: "",
  parts: [{ type: "compaction", id: "marker", auto: true }],
}
const summary: MessageWithParts = {
  id: "summary", role: "assistant", agent: "compaction", parent_id: "compact",
  session_id: "s", created_at: "", summary: false,
  parts: [{ type: "text", id: "summary-text", text: "## Goal\nInternal summary" }],
}

function props(messages: MessageWithParts[], streaming: boolean) {
  const [turn] = mergeTurns(messages)
  if (turn.kind !== "assistant") throw new Error("Expected process or assistant turn")
  return { messages: turn.messages, meta: turn.meta, streaming, sessionId: "s" }
}

describe("AssistantTurn context optimization", () => {
  it("shows a live optimization with no answer, reply actions or redundant thinking row", () => {
    render(<AssistantTurn {...props([request, summary], true)} />)
    expect(screen.getByRole("button", { name: /trace.compaction.title/, expanded: false })).toBeTruthy()
    expect(screen.queryByLabelText("final.title")).toBeNull()
    expect(screen.queryByTestId("reply-meta")).toBeNull()
    expect(screen.queryByText(/Internal summary/)).toBeNull()
  })

  it("keeps the optimization folded when an automatic loop resumes and answers", async () => {
    const { rerender } = render(<AssistantTurn {...props([request, summary], true)} />)
    const completed = { ...summary, summary: true, finish: "stop" }
    const next: MessageWithParts = {
      id: "next", role: "assistant", session_id: "s", created_at: "", parts: [],
    }
    rerender(<AssistantTurn {...props([request, completed, next], true)} />)
    expect(screen.queryByLabelText("final.title")).toBeNull()
    expect(screen.getByRole("button", { name: /trace.compaction.completed/, expanded: false })).toBeTruthy()

    next.finish = "stop"
    next.parts = [{ type: "text", id: "answer", text: "Continued successfully.", channel: "final" }]
    rerender(<AssistantTurn {...props([request, completed, next], false)} />)
    expect(screen.getByLabelText("final.title").textContent).toBe("Continued successfully.")
    expect(screen.getByTestId("reply-meta").getAttribute("data-message")).toBe("next")
    expect(screen.queryByText(/Internal summary/)).toBeNull()
  })

  it("keeps a stored manual summary out of final-answer and missing-answer presentation", () => {
    render(<AssistantTurn {...props([request, { ...summary, summary: true, finish: "stop" }], false)} />)
    expect(screen.getByRole("button", { name: /trace.compaction.completed/ })).toBeTruthy()
    expect(screen.queryByLabelText("final.title")).toBeNull()
    expect(screen.queryByText("final.missingTitle")).toBeNull()
    expect(screen.queryByTestId("reply-meta")).toBeNull()
  })

  it("groups thinking, tools and live optimization in one process without false live tool activity", () => {
    const step: MessageWithParts = {
      id: "tool-step", role: "assistant", session_id: "s", created_at: "", finish: "tool_calls",
      parts: [
        { type: "reasoning", id: "reasoning", text: "Inspect the source." },
        { type: "tool", id: "read", tool: "read", status: "completed", input: { path: "/test.txt" }, output: "Synthetic data." },
        { type: "step-finish", id: "finish", step: 1, input_tokens: 150000, output_tokens: 50, cost: 0, duration: 1 },
      ],
    }
    render(<AssistantTurn {...props([step, request, summary], true)} />)
    const process = within(screen.getByRole("region", { name: "trace.groupTitle" }))
    expect(process.getByRole("button", { name: /trace.think.titleDone/ })).toBeTruthy()
    expect(process.getByRole("button", { name: /trace.tool.titleDone/ })).toBeTruthy()
    expect(process.getByRole("button", { name: /trace.compaction.running/, expanded: false })).toBeTruthy()
    expect(screen.queryByLabelText("final.title")).toBeNull()
  })

  it("places manual optimization with the previous answer's process, before the answer", () => {
    const answer: MessageWithParts = {
      id: "answer", role: "assistant", session_id: "s", created_at: "", finish: "stop",
      parts: [{ type: "text", id: "answer-text", text: "Completed answer.", channel: "final" }],
    }
    const manual = { ...request, parts: [{ type: "compaction" as const, id: "marker", auto: false }] }
    const turns = mergeTurns([answer, manual, { ...summary, summary: true, finish: "stop" }])
    expect(turns).toHaveLength(1)
    render(<AssistantTurn {...props([answer, manual, { ...summary, summary: true, finish: "stop" }], false)} />)
    const process = screen.getByRole("region", { name: "trace.groupTitle" })
    expect(within(process).getByRole("button", { name: /trace.compaction.completed/ })).toBeTruthy()
    const final = screen.getByLabelText("final.title")
    expect(process.compareDocumentPosition(final) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByTestId("reply-meta").getAttribute("data-message")).toBe("answer")
  })
})
