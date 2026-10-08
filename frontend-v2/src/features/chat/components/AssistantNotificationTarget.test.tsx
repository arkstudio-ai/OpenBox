import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { afterEach, expect, it, vi } from "vitest"
import { AssistantNotificationTarget } from "./AssistantNotificationTarget"

const api = vi.hoisted(() => ({ target: vi.fn(), report: vi.fn(), task: vi.fn() }))
vi.mock("../api/assistant", () => ({
  useAssistantResultTarget: api.target, useAssistantTask: () => ({}),
  useAssistantResult: api.report, useRetryAssistantReport: () => ({ isPending: false }),
  useAssistantControl: () => ({ isPending: false }),
  useAssistantArchive: () => ({ isPending: false, mutate: vi.fn() }),
}))
vi.mock("../api/assistant-watch", () => ({ useAssistantWatch: () => ({ data: { items: [], has_more: false } }) }))
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key, i18n: { language: "en-US" } }) }))
vi.mock("@/shared/hooks/useApiErrorMessage", () => ({ useApiErrorMessage: () => () => "Unavailable" }))
afterEach(cleanup)

it("shows and opens the notification's older result while keeping current task controls", () => {
  api.target.mockReturnValue({ data: { task: { task: { id: "task", title: "Original task", execution_session_id: "execution",
    desired_state: "running", observed_state: "completed", intent_revision: 2, control_revision: 3, updated_at: new Date().toISOString() },
    execution_session: { id: "execution", status: "idle" }, latest_result: { result_id: "new", outcome: "succeeded" } },
    result: { result_id: "old", outcome: "error", delivery_state: "accepted", observed_intent_revision: 1 } } })
  api.report.mockReturnValue({ data: { pages: [{ offset: 0, sources: [{ part_id: "part", session_id: "execution", text: "Original error report" }] }] } })
  render(<MemoryRouter><AssistantNotificationTarget taskId="task" resultId="old" /></MemoryRouter>)
  expect(screen.getByText("assistant.status.failed")).toBeTruthy()
  expect(screen.getByText("assistant.card.note.earlier")).toBeTruthy()
  fireEvent.click(screen.getByRole("button", { name: "assistant.card.more" }))
  fireEvent.click(screen.getByRole("menuitem", { name: "assistant.card.showResult" }))
  expect(api.report).toHaveBeenCalledWith("old", true)
  expect(screen.getByText("Original error report")).toBeTruthy()
  expect(screen.getByRole("link", { name: /assistant.card.open/ }).getAttribute("href")).toBe("/app/s/execution")
})

it("replaces a cached target with the server's error when the refresh fails", () => {
  api.target.mockReturnValue({ error: new Error("gone"), data: { task: { task: { id: "task", title: "Private title" } } } })
  render(<MemoryRouter><AssistantNotificationTarget taskId="task" resultId="old" /></MemoryRouter>)
  expect(screen.getByRole("alert").textContent).toBe("Unavailable")
  expect(screen.queryByText("Private title")).toBeNull()
})

it("does not show another task's result for a mismatched notification link", () => {
  api.target.mockReturnValue({ data: { task: { task: { id: "other-task", title: "Other title" } }, result: {} } })
  render(<MemoryRouter><AssistantNotificationTarget taskId="task" resultId="old" /></MemoryRouter>)
  expect(screen.getByRole("alert").textContent).toBe("assistant.notificationUnavailable")
  expect(screen.queryByText("Other title")).toBeNull()
})
