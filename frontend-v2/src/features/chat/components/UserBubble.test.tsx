import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, render, screen } from "@testing-library/react"
import type { MessageWithParts, TextPart } from "@/shared/types/api"
import { UserBubble } from "./UserBubble"

vi.mock("react-i18next", async (original) => ({ ...await original<typeof import("react-i18next")>(),
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: "en-US" } }),
}))
vi.mock("./Markdown", () => ({ default: ({ text }: { text: string }) => <p>{text}</p> }))
afterEach(cleanup)

function bubble(part: Partial<TextPart>, sessionId = "project-session"): MessageWithParts {
  return { id: "input", session_id: sessionId, role: "user", created_at: "2026-10-06T00:00:00Z",
    parts: [{ id: "text", type: "text", text: "Build the snake page", ...part }] }
}

describe("user bubble origin", () => {
  it("shows an instruction the personal assistant sent, badged, in an ordinary project session", async () => {
    render(<UserBubble message={bubble({ synthetic: true, origin: "assistant_delegation", origin_ref: { task_id: "task" } })} />)
    expect(await screen.findByText("Build the snake page")).toBeTruthy()
    expect(screen.getByText("message.sentByAssistant")).toBeTruthy()
  })

  it("shows the badge in an assistant task session too", async () => {
    render(<UserBubble message={bubble({ synthetic: true, origin: "assistant_delegation" }, "execution")} />)
    expect(await screen.findByText("Build the snake page")).toBeTruthy()
    expect(screen.getByText("message.sentByAssistant")).toBeTruthy()
  })

  it.each([
    ["a human message", { origin: "human" as const }],
    ["an older message without an origin", {}],
    ["an unknown origin", { origin: "unknown" as const }],
  ])("does not badge %s", async (_label, part) => {
    render(<UserBubble message={bubble(part)} />)
    expect(await screen.findByText("Build the snake page")).toBeTruthy()
    expect(screen.queryByText("message.sentByAssistant")).toBeNull()
  })

  it.each(["task_result", "system_recovery"] as const)("keeps a synthetic %s input hidden and unbadged", (origin) => {
    const view = render(<UserBubble message={bubble({ synthetic: true, origin })} />)
    expect(view.container.textContent).toBe("")
  })
})
