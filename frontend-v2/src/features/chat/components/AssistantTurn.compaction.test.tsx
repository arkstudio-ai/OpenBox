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

describe("AssistantTurn context optimization", () => {
  it("does not attach a later visible coordination step's badges to a saved report", async () => {
    const answer: MessageWithParts = {
      id: "report", role: "assistant", session_id: "s", created_at: "2026-10-05T00:00:00Z", finish: "stop",
      source_status: "available", reaction: "up",
      parts: [{ id: "report-text", type: "text", channel: "final", text: "Saved report." }],
    }
    const coordinating: MessageWithParts = {
      ...answer, id: "coordinating", created_at: "2026-10-05T00:01:00Z", finish: "tool_calls", reaction: "down",
      parts: [{ id: "read", type: "tool", tool: "read", status: "running", input: { path: "/source" } }],
    }
    const context = { transcript: new Map([[answer.id, answer], [coordinating.id, coordinating]]), sourcesAvailable: true }
    render(<AssistantReadContext.Provider value={context}>
      <AssistantTurn {...props([answer, coordinating], true)} />
    </AssistantReadContext.Provider>)
    await waitFor(() => expect(within(screen.getByLabelText("final.title")).getByText("Saved report.").getAttribute("data-streaming")).toBe("false"))
    const meta = screen.getByTestId("reply-meta")
    expect(meta.getAttribute("data-message")).toBe(answer.id)
    expect(meta.getAttribute("data-created")).toBe(answer.created_at)
    expect(meta.getAttribute("data-streaming")).toBe("false")
    expect(meta.getAttribute("data-reaction")).toBe("up")
    expect(screen.getByRole("button", { name: /trace.tool.title/ })).toBeTruthy()
  })

  it.each(["pending", "unavailable"] as const)("keeps the previous answer settled while the newest source is %s", async (status) => {
    const first: MessageWithParts = {
      id: "first", role: "assistant", session_id: "s", created_at: "2026-10-05T00:00:00Z", finish: "stop",
      source_status: "available", source_checked_at: "2026-10-05T00:01:00Z", reaction: "up",
      tokens: { input: 10, output: 20, cache: 0, total: 30, limit: 1000, cost: 0, context: 10 },
      parts: [{ id: "first-text", type: "text", channel: "final", text: "First completed report." }],
    }
    const latest: MessageWithParts = {
      ...first, id: "latest", created_at: "2026-10-05T00:02:00Z", finish: null, reaction: "down",
      tokens: { input: 100, output: 200, cache: 0, total: 300, limit: 1000, cost: 0, context: 100 },
      error: { message: "PRIVATE_ERROR" },
      parts: [{ id: "latest-text", type: "text", channel: "final", text: "Next report." }],
    }
    const hidden = { ...latest, parts: [], source_status: status, source_checked_at: "2026-10-05T00:03:00Z" }
    const context = { transcript: new Map([[first.id, first], [latest.id, hidden]]), sourcesAvailable: true }
    const view = render(<AssistantReadContext.Provider value={context}>
      <AssistantTurn {...props([first, latest], true)} />
    </AssistantReadContext.Provider>)
    const final = within(screen.getByLabelText("final.title"))
    await waitFor(() => expect(final.getByText("First completed report.").getAttribute("data-streaming")).toBe("false"))
    const meta = screen.getByTestId("reply-meta")
    expect(meta.getAttribute("data-message")).toBe(first.id)
    expect(meta.getAttribute("data-created")).toBe(first.created_at)
    expect(meta.getAttribute("data-streaming")).toBe("false")
    expect(meta.getAttribute("data-tokens")).toBe(JSON.stringify(first.tokens))
    expect(meta.getAttribute("data-reaction")).toBe("up")
    expect(view.container.textContent).not.toContain("PRIVATE_ERROR")
    expect(view.container.textContent).not.toContain("Next report.")

    // Once the next source is verified, its own text and live metadata return.
    const available = { ...latest, error: undefined, source_checked_at: hidden.source_checked_at }
    view.rerender(<AssistantReadContext.Provider value={{ ...context, transcript: new Map([[first.id, first], [latest.id, available]]) }}>
      <AssistantTurn {...props([first, latest], true)} />
    </AssistantReadContext.Provider>)
    await waitFor(() => expect(final.getByText("Next report.").getAttribute("data-streaming")).toBe("true"))
    expect(screen.getByTestId("reply-meta").getAttribute("data-created")).toBe(latest.created_at)
    expect(view.container.textContent).not.toContain("PRIVATE_ERROR")
  })

  it("renders the durable safe failure receipt without restoring a cached provider error", () => {
    const stored: MessageWithParts = {
      id: "failed", role: "assistant", session_id: "s", created_at: "", finish: "error",
      error: { code: "ASSISTANT_TURN_BUDGET", message: "PRIVATE_ERROR_DETAILS" },
      parts: [{ id: "partial", type: "text", text: "UNVERIFIED_PARTIAL" }],
    }
    const receipt = { ...stored, parts: [], source_status: "available" as const,
      source_checked_at: "2026-10-04T00:00:00+00:00",
      error: { code: "ASSISTANT_TURN_BUDGET", message: "Turn stopped at its limit; completed actions are retained." } }
    const snapshot = { answers: [] } as unknown as AssistantSnapshot
    const context = { snapshot, transcript: new Map([[stored.id, receipt]]), displayed: vi.fn(), sourcesAvailable: true }
    const view = render(<AssistantReadContext.Provider value={context}>
      <AssistantTurn {...props([stored], false)} />
    </AssistantReadContext.Provider>)
    expect(screen.getByRole("alert").textContent).toBe(receipt.error.message)
    expect(view.container.textContent).not.toContain("PRIVATE_ERROR_DETAILS")
    expect(view.container.textContent).not.toContain("UNVERIFIED_PARTIAL")
    view.rerender(<AssistantReadContext.Provider value={{ ...context, sourcesAvailable: false }}>
      <AssistantTurn {...props([stored], false)} />
    </AssistantReadContext.Provider>)
    expect(screen.queryByRole("alert")).toBeNull()
  })
  it("removes revoked text, process details and copy actions while preserving a separate valid answer", () => {
    const secret: MessageWithParts = {
      id: "old-answer", role: "assistant", session_id: "s", created_at: "", finish: "stop",
      source_status: "available", source_checked_at: "2026-10-03T10:00:00.000000+00:00",
      parts: [{ id: "private-text", type: "text", text: "PRIVATE_ANSWER", channel: "final" },
        { id: "private-reason", type: "reasoning", text: "PRIVATE_REASONING" },
        { id: "private-tool", type: "tool", tool: "read", status: "completed", input: { path: "PRIVATE_PATH" }, output: "PRIVATE_TOOL_OUTPUT" }],
    }
    const valid: MessageWithParts = { ...secret, id: "valid-answer", parts: [
      { id: "valid-text", type: "text", text: "Still authorized answer", channel: "final" }],
    }
    const snapshot = { answers: [] } as unknown as AssistantSnapshot
    const transcript = new Map([[secret.id, { ...secret, source_status: "unavailable" as const, parts: [],
      source_checked_at: "2026-10-03T10:00:01.000000+00:00" }], [valid.id, valid]])
    const context = { snapshot, transcript, displayed: vi.fn(), sourcesAvailable: true }
    const view = render(<AssistantReadContext.Provider value={context}>
      <AssistantTurn {...props([secret, valid], false)} />
    </AssistantReadContext.Provider>)
    expect(view.container.textContent).not.toContain("PRIVATE_")
    expect(screen.getByText("assistant.sourceUnavailable")).toBeTruthy()
    expect(screen.getByTestId("reply-meta").getAttribute("data-message")).toBe("valid-answer")
    view.rerender(<AssistantReadContext.Provider value={{ ...context, sourcesAvailable: false }}>
      <AssistantTurn {...props([secret, valid], false)} />
    </AssistantReadContext.Provider>)
    expect(screen.queryByTestId("reply-meta")).toBeNull()
    expect(view.container.textContent).not.toContain("Still authorized answer")
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
