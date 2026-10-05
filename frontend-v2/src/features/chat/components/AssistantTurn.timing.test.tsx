import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import type { MessageWithParts } from "@/shared/types/api"
import type { AssistantSnapshot } from "../api/assistant"
import { mergeTurns } from "../lib/turn-view"
import { AssistantReadContext } from "../hooks/assistant-read-context"
import { AssistantTurn } from "./AssistantTurn"

vi.mock("react-i18next", async (original) => ({ ...await original<typeof import("react-i18next")>(),
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: "en-US" } }),
}))
vi.mock("../api/config", () => ({ useConfigQuery: () => ({}) }))
vi.mock("../api/message-actions", () => ({
  useSessionQuery: () => ({}), usePreserveAssistantEvidence: () => true,
  useSetReaction: () => ({}), useForkMessage: () => ({}), useRegenerate: () => ({}),
}))
vi.mock("../hooks/useVerifiedAssistantCopy", () => ({ useVerifiedAssistantCopy: () => ({ copyReply: vi.fn() }) }))
vi.mock("./Markdown", () => ({ default: ({ text }: { text: string }) => <p>{text}</p> }))
afterEach(cleanup)

const answer: MessageWithParts = {
  id: "report", session_id: "main", role: "assistant", finish: "stop",
  created_at: "2026-10-05T00:01:01.674Z", source_status: "available",
  source_checked_at: "2026-10-05T01:00:00Z",
  assistant_timing: { accepted_at: "2026-10-05T00:00:00.000000+00:00", settled_at: "2026-10-05T00:01:06.474375+00:00" },
  parts: [{ id: "text", type: "text", text: "Completed report.", channel: "final" },
    { id: "step", type: "step-finish", step: 1, duration: 4.8, input_tokens: 10, output_tokens: 5, cost: 0 }],
}
const snapshot = { answers: [], session: { id: "main" } } as unknown as AssistantSnapshot

function turn(messages: MessageWithParts[], scope: "main" | "execution" | "ordinary" = "main") {
  const [group] = mergeTurns(messages)
  if (group.kind !== "assistant") throw new Error("Expected assistant group")
  const context = scope === "ordinary" ? null : { snapshot: scope === "main" ? snapshot : undefined,
    sourcesAvailable: true, transcript: new Map(messages.map((message) => [message.id, message])) }
  return <MemoryRouter><AssistantReadContext.Provider value={context}>
    <AssistantTurn sessionId="main" messages={group.messages} meta={group.meta} streaming={false} />
  </AssistantReadContext.Provider></MemoryRouter>
}

describe("main assistant reply elapsed time", () => {
  it("uses the answer's accepted-to-settled time through the real metadata badge", () => {
    render(turn([answer]))
    expect(screen.getByLabelText("assistant.replyDuration").textContent).toBe("1m 6s")
    expect(screen.queryByLabelText("meta.totalDuration")).toBeNull()
    expect(screen.queryByText("4.8s")).toBeNull()
  })

  it("does not borrow a later coordination run or add merged runs to the visible report", () => {
    const next = { ...answer, id: "coordination", finish: "tool_calls", assistant_timing: {
      accepted_at: "2026-10-05T00:03:00Z", settled_at: "2026-10-05T00:12:00Z" }, parts: [
      { id: "next-step", type: "step-finish" as const, step: 1, duration: 90, input_tokens: 0, output_tokens: 0, cost: 0 }],
    }
    render(turn([answer, next]))
    expect(screen.getByLabelText("assistant.replyDuration").textContent).toBe("1m 6s")
  })

  it.each([undefined, { accepted_at: "invalid", settled_at: "2026-10-05T00:00:00Z" },
    { accepted_at: "2026-10-05T00:02:00Z", settled_at: "2026-10-05T00:01:00Z" }])(
    "does not infer a completed duration when settlement timing is missing or invalid", (assistant_timing) => {
      render(turn([{ ...answer, assistant_timing }]))
      expect(screen.queryByLabelText("assistant.replyDuration")).toBeNull()
      expect(screen.queryByLabelText("meta.totalDuration")).toBeNull()
    })

  it.each(["pending", "unavailable"] as const)("hides held timing along with %s source content", (source_status) => {
    const view = render(turn([answer]))
    expect(screen.getByLabelText("assistant.replyDuration")).toBeTruthy()
    view.rerender(turn([{ ...answer, source_status, parts: [] }]))
    expect(screen.queryByLabelText("assistant.replyDuration")).toBeNull()
  })

  it.each(["ordinary", "execution"] as const)("keeps %s chat's existing step-duration convention", (scope) => {
    render(turn([answer], scope))
    expect(screen.getByLabelText("meta.totalDuration").textContent).toBe("4.8s")
    expect(screen.queryByLabelText("assistant.replyDuration")).toBeNull()
  })
})
