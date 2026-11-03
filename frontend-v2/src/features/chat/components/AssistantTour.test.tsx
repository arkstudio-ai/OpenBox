import { act, cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { DEFAULT_ASSISTANT_META } from "@/shared/appearance/assistant-profile"
import { useAppearanceStore } from "@/shared/appearance/store"
import { useAssistantTour } from "../stores/assistant-tour"
import { AssistantTourHost } from "./AssistantTour"

vi.mock("react-i18next", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-i18next")>()),
  useTranslation: () => ({ t: (key: string, o?: { index?: number }) => (o?.index ? `${key}:${o.index}` : key) }),
}))

const recordIntro = vi.fn(async () => undefined)

beforeEach(() => {
  recordIntro.mockClear()
  useAssistantTour.setState({ open: false })
  useAppearanceStore.setState({ recordIntro, assistantMeta: { ...DEFAULT_ASSISTANT_META, intro: { status: "done", steps: {}, nudged: false, tourSeen: false } } })
})
afterEach(cleanup)

it("opens by itself once for someone who has not seen it, walks four steps and records it on finish", () => {
  render(<AssistantTourHost />)
  expect(screen.getByText("assistant.tour.steps.talk.title")).toBeTruthy()
  fireEvent.click(screen.getByText("assistant.tour.next"))
  fireEvent.click(screen.getByText("assistant.tour.next"))
  fireEvent.click(screen.getByText("assistant.tour.next"))
  expect(screen.getByText("assistant.tour.steps.report.title")).toBeTruthy()
  fireEvent.click(screen.getByText("assistant.tour.done"))
  expect(screen.queryByTestId("assistant-tour")).toBeNull()
  expect(recordIntro).toHaveBeenCalledWith({ event: "tour" })
})

it("stays closed for someone who has seen it, or while the first meeting still asks, until asked for", () => {
  useAppearanceStore.setState({ assistantMeta: { ...DEFAULT_ASSISTANT_META, intro: { status: "new", steps: {}, nudged: false, tourSeen: false } } })
  render(<AssistantTourHost />)
  expect(screen.queryByTestId("assistant-tour")).toBeNull()
  cleanup()
  useAppearanceStore.setState({ assistantMeta: { ...DEFAULT_ASSISTANT_META, intro: { status: "done", steps: {}, nudged: false, tourSeen: true } } })
  render(<AssistantTourHost />)
  expect(screen.queryByTestId("assistant-tour")).toBeNull()
  act(() => useAssistantTour.getState().setOpen(true))
  expect(screen.getByTestId("assistant-tour")).toBeTruthy()
  fireEvent.click(screen.getByText("assistant.tour.skip"))
  expect(recordIntro).not.toHaveBeenCalled()
})
