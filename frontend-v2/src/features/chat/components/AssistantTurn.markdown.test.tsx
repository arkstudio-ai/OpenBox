import { cleanup, render, screen, within } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { MemoryRouter } from "react-router"
import type { MessageWithParts } from "@/shared/types/api"
import { mergeTurns } from "../lib/turn-view"
import { AssistantTurn } from "./AssistantTurn"

vi.mock("react-i18next", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-i18next")>()),
  useTranslation: () => ({ t: (key: string) => key }),
}))
vi.mock("./meta/AssistantMeta", () => ({ AssistantMeta: () => null }))

afterEach(cleanup)

function report(id: string, result: string, text: string): MessageWithParts {
  return {
    id, role: "assistant", session_id: "s", created_at: "", finish: "stop",
    parts: [{ id: `${id}-text`, type: "text", channel: "final",
      text: `### Report\n\n- Result: \`${result}\`\n\n${text}` }],
  }
}

function turn(messages: MessageWithParts[], streaming = false) {
  const [value] = mergeTurns(messages)
  if (value.kind !== "assistant") throw new Error("Expected an assistant turn")
  return <MemoryRouter><AssistantTurn sessionId="s" messages={value.messages}
    meta={value.meta} streaming={streaming} /></MemoryRouter>
}

describe("AssistantTurn Markdown identity", () => {
  it.each([false, true])("shows the new report's inline IDs when the final message changes (streaming=%s)", async (streaming) => {
    // Same-length IDs occupy identical Markdown source positions. Use the
    // real renderer: a string-only mock misses its node-position memoization.
    const oldId = "result-11111111111111111111"
    const newId = "result-22222222222222222222"
    const first = report("old-report", oldId, "Original report.")
    const second = report("new-report", newId, "Updated report.")
    const { rerender } = render(turn([first]))
    expect(await within(screen.getByLabelText("final.title")).findByText(oldId)).toBeTruthy()
    rerender(turn([first, second], streaming))
    const final = within(screen.getByLabelText("final.title"))
    expect(await final.findByText(newId)).toBeTruthy()
    expect(final.queryByText(oldId)).toBeNull()
    expect(screen.getByLabelText("final.title").textContent).toContain("Updated report.")
  })
})
