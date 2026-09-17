import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import type { CompactionView } from "../lib/compaction-view"
import { CompactionTrace } from "./CompactionTrace"

vi.mock("react-i18next", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-i18next")>()),
  useTranslation: () => ({ t: (key: string) => key }),
}))
vi.mock("./Markdown", () => ({ default: ({ text }: { text: string }) => <p>{text}</p> }))

afterEach(cleanup)

const item: CompactionView = { id: "optimization", status: "running", summary: "## Goal\nRetain the task." }

describe("the context optimization fold", () => {
  it("keeps streamed and completed summaries out of the page until expanded", async () => {
    const { rerender } = render(<CompactionTrace item={item} />)
    const button = screen.getByRole("button", { expanded: false })
    expect(screen.queryByText(/Retain the task/)).toBeNull()
    const panel = document.getElementById(button.getAttribute("aria-controls")!)
    expect(panel?.getAttribute("aria-hidden")).toBe("true")

    fireEvent.click(button)
    expect(await screen.findByText(/Retain the task/)).toBeTruthy()
    rerender(<CompactionTrace item={{ ...item, status: "completed" }} />)
    expect(screen.getByRole("button", { expanded: true })).toBeTruthy()
    fireEvent.click(button)
    expect(screen.queryByText(/Retain the task/)).toBeNull()
  })

  it.each(["failed", "interrupted"] as const)("shows %s in the folded row without showing its partial text", (status) => {
    render(<CompactionTrace item={{ ...item, status }} />)
    expect(screen.getByRole("button").textContent).toContain(`trace.compaction.${status}`)
    expect(screen.queryByText(/Retain the task/)).toBeNull()
  })

  it("does not open itself when new deltas or completion arrive", () => {
    const { rerender } = render(<CompactionTrace item={{ ...item, summary: "" }} />)
    rerender(<CompactionTrace item={item} />)
    rerender(<CompactionTrace item={{ ...item, status: "completed" }} />)
    expect(screen.getByRole("button", { expanded: false })).toBeTruthy()
    expect(screen.queryByText(/Retain the task/)).toBeNull()
  })
})
