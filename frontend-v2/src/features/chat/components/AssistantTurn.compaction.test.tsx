import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, render, screen, waitFor, within } from "@testing-library/react"
import type { MessageWithParts } from "@/shared/types/api"
import { mergeTurns } from "../lib/turn-view"
import { AssistantTurn } from "./AssistantTurn"
import { AssistantReadContext } from "../hooks/assistant-read-context"
import type { AssistantSnapshot } from "../api/assistant"

vi.mock("react-i18next", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-i18next")>()),
  useTranslation: () => ({ t: (key: string) => key }),
}))
vi.mock("./Markdown", () => ({ default: ({ text, streaming }: { text: string; streaming: boolean }) =>
  <p data-streaming={String(streaming)}>{text}</p> }))
vi.mock("./meta/AssistantMeta", () => ({
  AssistantMeta: ({ messageId, content, createdAt, streaming, tokens, reaction }: {
    messageId: string; content: string; createdAt: string; streaming: boolean;
    tokens?: MessageWithParts["tokens"]; reaction?: MessageWithParts["reaction"]
  }) => <div data-testid="reply-meta" data-message={messageId} data-created={createdAt}
    data-streaming={String(streaming)} data-tokens={JSON.stringify(tokens)} data-reaction={reaction}>{content}</div>,
}))
vi.mock("./meta/InlineErrorCard", () => ({
  InlineErrorCard: ({ error }: { error: Record<string, unknown> }) => <p role="alert">{String(error.message)}</p>,
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

/** The main assistant page's context, for session "s". */
const mainPage = { snapshot: { answers: [], session: { id: "s" } } as unknown as AssistantSnapshot, displayed: vi.fn() }

describe("AssistantTurn context optimization", () => {
  it("does not attach a later visible coordination step's badges to a saved report", async () => {
    const answer: MessageWithParts = {
      id: "report", role: "assistant", session_id: "s", created_at: "2026-10-05T00:00:00Z", finish: "stop",
      reaction: "up",
      parts: [{ id: "report-text", type: "text", channel: "final", text: "Saved report." }],
    }
    const coordinating: MessageWithParts = {
      ...answer, id: "coordinating", created_at: "2026-10-05T00:01:00Z", finish: "tool_calls", reaction: "down",
      parts: [{ id: "read", type: "tool", tool: "read", status: "running", input: { path: "/source" } }],
    }
    render(<AssistantReadContext.Provider value={mainPage}>
      <AssistantTurn {...props([answer, coordinating], true)} />
    </AssistantReadContext.Provider>)
    await waitFor(() => expect(within(screen.getByLabelText("final.title")).getByText("Saved report.").getAttribute("data-streaming")).toBe("false"))
    const meta = screen.getByTestId("reply-meta")
    expect(meta.getAttribute("data-message")).toBe(answer.id)
    expect(meta.getAttribute("data-created")).toBe(answer.created_at)
    expect(meta.getAttribute("data-streaming")).toBe("false")
    expect(meta.getAttribute("data-reaction")).toBe("up")
    // The personal assistant keeps its tool calls off the page.
    expect(screen.queryByRole("button", { name: /trace.tool.title/ })).toBeNull()
  })

  it("streams the newest answer on the main page as soon as its text arrives", async () => {
    const first: MessageWithParts = {
      id: "first", role: "assistant", session_id: "s", created_at: "2026-10-05T00:00:00Z", finish: "stop",
      parts: [{ id: "first-text", type: "text", channel: "final", text: "First completed report." }],
    }
    const latest: MessageWithParts = {
      ...first, id: "latest", created_at: "2026-10-05T00:02:00Z", finish: null,
      parts: [{ id: "latest-text", type: "text", channel: "final", text: "Next report." }],
    }
    const view = render(<AssistantReadContext.Provider value={mainPage}>
      <AssistantTurn {...props([first, latest], true)} />
    </AssistantReadContext.Provider>)
    const final = within(screen.getByLabelText("final.title"))
    await waitFor(() => expect(final.getByText("Next report.").getAttribute("data-streaming")).toBe("true"))
    expect(screen.getByTestId("reply-meta").getAttribute("data-created")).toBe(latest.created_at)
    expect(view.container.textContent).not.toContain("assistant.source")
  })

  it("puts a memory chip under the answer and keeps the raw call off the page", () => {
    const answer: MessageWithParts = {
      id: "answer", role: "assistant", session_id: "s", created_at: "", finish: "stop",
      parts: [
        { id: "remember", type: "tool", tool: "memory.remember", status: "completed", input: { summary: "Prefers tables" },
          output: JSON.stringify({ state: "remembered", memory_id: "m1", summary: "Prefers tables", revision: 1 }) },
        { id: "answer-text", type: "text", text: "Noted, tables from now on.", channel: "final" },
      ],
    }
    render(<AssistantReadContext.Provider value={mainPage}><AssistantTurn {...props([answer], false)} /></AssistantReadContext.Provider>)
    const final = screen.getByLabelText("final.title")
    const chips = screen.getByRole("group", { name: "assistant.memory.label" })
    expect(chips.textContent).toContain("assistant.memory.remembered")
    expect(final.compareDocumentPosition(chips) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.queryByRole("region", { name: "trace.groupTitle" })).toBeNull()
    expect(screen.queryByRole("button", { name: /trace.tool.title/ })).toBeNull()
  })

  it("speaks as the assistant, says what it is doing, and labels a task update", () => {
    const working: MessageWithParts = {
      id: "working", role: "assistant", session_id: "s", created_at: "", finish: null,
      parts: [{ id: "list", type: "tool", tool: "tasks.list", status: "running", input: {} }],
    }
    const report: MessageWithParts = { id: "update", role: "user", session_id: "s", created_at: "",
      parts: [{ id: "input", type: "text", text: "Report this task result", synthetic: true, origin: "task_result" }] }
    const [group] = mergeTurns([report, working])
    if (group?.kind !== "assistant") throw new Error("Expected assistant group")
    render(<AssistantReadContext.Provider value={mainPage}>
      <AssistantTurn messages={group.messages} meta={group.meta} streaming sessionId="s" origin={group.origin} />
    </AssistantReadContext.Provider>)
    expect(screen.getByText("assistant.name")).toBeTruthy()
    expect(screen.getByText("assistant.origin.report")).toBeTruthy()
    expect(screen.getByRole("status").textContent).toBe("assistant.activity.checkingWork")
    expect(screen.queryByText("tasks.list")).toBeNull()
  })

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

describe("personal assistant answers written before the plain-voice prompt", () => {
  it("does not show the internal ids an older answer named", () => {
    const answer: MessageWithParts = {
      id: "old", role: "assistant", session_id: "s", created_at: "", finish: "stop",
      parts: [{ id: "old-text", type: "text", channel: "final",
        text: "任务「收尾自检」（ID: `01M48Y8NP3008Z51ZE6QEH50VZ`）已完成（outcome: succeeded）。" }],
    }
    render(<AssistantReadContext.Provider value={mainPage}><AssistantTurn {...props([answer], false)} /></AssistantReadContext.Provider>)
    expect(screen.getByLabelText("final.title").textContent).toBe("任务「收尾自检」已完成。")
    expect(screen.getByTestId("reply-meta").textContent).toBe("任务「收尾自检」已完成。")
  })

  it("leaves another session's answers exactly as written", () => {
    const answer: MessageWithParts = {
      id: "work", role: "assistant", session_id: "s", created_at: "", finish: "stop",
      parts: [{ id: "work-text", type: "text", channel: "final", text: "Run 01M48Y8NP3008Z51ZE6QEH50VZ (outcome: succeeded)" }],
    }
    render(<AssistantTurn {...props([answer], false)} />)
    expect(screen.getByLabelText("final.title").textContent).toBe("Run 01M48Y8NP3008Z51ZE6QEH50VZ (outcome: succeeded)")
  })
})
