// A desktop takeover reads back as the question it filed; one that failed
// before filing anything still says what went wrong.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { cleanup, render, screen } from "@testing-library/react"
import type { ToolPart } from "@/shared/types/api"
import { ToolOutput } from "./ToolOutput"

vi.mock("react-i18next", async (original) => ({
  ...(await original<typeof import("react-i18next")>()),
  useTranslation: () => ({ t: (key: string) => key }),
}))

beforeEach(() => {
  // The detail text measures itself; jsdom has no ResizeObserver.
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  )
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function takeover(fields: Partial<ToolPart>): ToolPart {
  return {
    type: "tool",
    id: "p1",
    tool: "desktop_takeover",
    status: "completed",
    input: { reason: "captcha_slider", url: "https://login.taobao.com/" },
    ...fields,
  }
}

describe("a desktop takeover's detail", () => {
  it("shows the question it filed and the answer", () => {
    render(
      <ToolOutput
        part={takeover({ metadata: { questions: ["Finished the check?"], answers: [["Done"]] } })}
      />,
    )

    expect(screen.getByText("Finished the check?")).toBeTruthy()
    expect(screen.getByText("Done")).toBeTruthy()
  })

  it("shows the error when it failed before filing its question", () => {
    render(<ToolOutput part={takeover({ status: "error", error: "desktop unreachable", metadata: {} })} />)

    expect(screen.getByText("toolDetail.error")).toBeTruthy()
    expect(screen.getByText("desktop unreachable")).toBeTruthy()
  })
})

describe("a memory read's detail", () => {
  it("says the memory text is not kept instead of showing the stored references", () => {
    const output = JSON.stringify({
      status: "stored_without_text",
      references: [{ kind: "memory", id: "memory_1", revision: 2 }],
      note: "Memory text is read fresh for the assistant and never kept in chat history.",
    })
    render(
      <ToolOutput
        part={{ type: "tool", id: "p2", tool: "memory_search", status: "completed", input: { query: "过敏" }, output }}
      />,
    )

    expect(screen.getByText("toolDetail.memoryNotKept")).toBeTruthy()
    expect(screen.queryByText(/memory_1/)).toBeNull()
  })

  it("leaves other results untouched", () => {
    render(
      <ToolOutput
        part={{ type: "tool", id: "p3", tool: "mcp_notes", status: "completed", input: {}, output: '{"status":"ok"}' }}
      />,
    )

    expect(screen.getByText('{"status":"ok"}')).toBeTruthy()
  })
})
