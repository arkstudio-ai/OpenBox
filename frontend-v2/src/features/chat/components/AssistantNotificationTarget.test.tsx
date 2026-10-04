import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { afterEach, expect, it, vi } from "vitest"
import { AssistantNotificationTarget } from "./AssistantNotificationTarget"

const api = vi.hoisted(() => ({ target: vi.fn(), report: vi.fn(), task: vi.fn() }))
vi.mock("../api/assistant", () => ({
  useAssistantResultTarget: api.target, useAssistantTask: () => ({}),
  useAssistantResult: api.report, useRetryAssistantReport: () => ({ isPending: false }),
  useAssistantControl: () => ({ isPending: false }),
}))
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
vi.mock("@/shared/hooks/useApiErrorMessage", () => ({ useApiErrorMessage: () => () => "Unavailable" }))
afterEach(cleanup)

it("shows and opens the notification's older result while keeping current task controls", () => {
  api.target.mockReturnValue({ data: { task: { task: { id: "task", title: "Original task", execution_session_id: "execution",
    desired_state: "running", observed_state: "completed", intent_revision: 2, control_revision: 3 },
    execution_session: { id: "execution", status: "idle" }, latest_result: { result_id: "new", outcome: "succeeded" } },
    result: { result_id: "old", outcome: "error", delivery_state: "accepted", observed_intent_revision: 1 } } })
  api.report.mockReturnValue({ data: { pages: [{ offset: 0, sources: [{ part_id: "part", session_id: "execution", text: "Original error report" }] }] } })
  render(<MemoryRouter><AssistantNotificationTarget taskId="task" resultId="old" /></MemoryRouter>)
  expect(screen.getByText("assistant.executionFailed")).toBeTruthy()
  expect(screen.getByText("assistant.earlierResult")).toBeTruthy()
  fireEvent.click(screen.getByText("assistant.originalReport"))
  expect(api.report).toHaveBeenCalledWith("old", true)
  expect(screen.getByText("Original error report")).toBeTruthy()
  expect(screen.getByRole("link", { name: "assistant.openTask" }).getAttribute("href")).toBe("/app/s/execution")
})

it("hides a cached target after source revocation", () => {
  api.target.mockReturnValue({ error: new Error("revoked"), data: { task: { task: { id: "task", title: "Private title" } } } })
  render(<MemoryRouter><AssistantNotificationTarget taskId="task" resultId="old" /></MemoryRouter>)
  expect(screen.getByRole("alert").textContent).toBe("assistant.sourceUnavailable")
  expect(screen.queryByText("Private title")).toBeNull()
})
