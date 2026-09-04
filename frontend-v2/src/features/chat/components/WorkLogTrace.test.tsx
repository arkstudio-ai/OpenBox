import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, render, screen } from "@testing-library/react"
import type { WorkEvent } from "../lib/content-view"
import { WorkLogTrace } from "./WorkLogTrace"

// Only `useTranslation` is stubbed: the shared i18n module is pulled in
// transitively and still needs the real `initReactI18next`.
vi.mock("react-i18next", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-i18next")>()),
  useTranslation: () => ({
    t: (key: string) =>
      ({
        "trace.work.titleActive": "Working",
        "trace.work.titleDone": "Work log",
      })[key] ?? key,
  }),
}))

vi.mock("@/shared/api/assets", () => ({ useAssetUrl: vi.fn(() => ({ data: undefined })) }))

function narration(id: string, text: string, order: number): WorkEvent {
  return { kind: "narration", id, order, text }
}

afterEach(cleanup)

describe("WorkLogTrace", () => {
  it("renders nothing when the turn produced no work", () => {
    const { container } = render(<WorkLogTrace events={[]} streaming={false} />)
    expect(container.innerHTML).toBe("")
  })

  // The log is the turn's only account of what it did. Behind a fold it read
  // as if the turn had said nothing at all.
  it("shows the narration without making the reader expand anything", () => {
    render(<WorkLogTrace events={[narration("a", "Compressing before upload.", 1)]} streaming={false} />)

    expect(screen.getByText("Compressing before upload.")).toBeTruthy()
    expect(screen.queryByRole("button", { expanded: false })).toBeNull()
  })

  // `finalMessageIndex` reclassifies prose between "final" and "progress" as a
  // turn streams, so an earlier paragraph must survive the arrival of a later
  // one rather than being replaced by it.
  it("keeps every earlier narration once a newer one arrives", () => {
    const events = [
      narration("a", "First step.", 1),
      narration("b", "Second step.", 2),
      narration("c", "Third step.", 3),
    ]
    render(<WorkLogTrace events={events} streaming />)

    for (const text of ["First step.", "Second step.", "Third step."]) {
      expect(screen.getByText(text)).toBeTruthy()
    }
    expect(screen.getAllByRole("listitem")).toHaveLength(3)
  })

  it("marks the log live while the turn is still working", () => {
    const { rerender } = render(<WorkLogTrace events={[narration("a", "Step.", 1)]} streaming />)
    expect(screen.getByLabelText("Working")).toBeTruthy()

    rerender(<WorkLogTrace events={[narration("a", "Step.", 1)]} streaming={false} />)
    expect(screen.getByLabelText("Work log")).toBeTruthy()
  })
})
