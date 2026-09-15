// A desktop takeover reads back as the question it filed; one that failed
// before filing anything still says what went wrong.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { cleanup, render, screen } from "@testing-library/react"
import type { ToolPart } from "@/shared/types/api"
import { ToolOutput } from "./ToolOutput"

vi.mock("react-i18next", () => ({
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
