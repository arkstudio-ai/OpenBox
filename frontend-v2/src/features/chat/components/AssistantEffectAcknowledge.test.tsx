import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import type { AssistantTaskView } from "../api/assistant"
import { toast } from "@/shared/ui/Toast"
import { ApiError } from "@/shared/api/http"
import { requestEffectAcknowledge, useEffectAcknowledgeRequests } from "../stores/effect-acknowledge"
import { AssistantEffectAcknowledge } from "./AssistantEffectAcknowledge"
import { AssistantTaskCard } from "./AssistantTaskCard"

const api = vi.hoisted(() => ({ acknowledge: vi.fn(), control: vi.fn(), task: vi.fn() }))
vi.mock("../api/assistant", () => ({
  useAssistantControl: () => ({ isPending: false, mutateAsync: api.control }),
  useAssistantTask: api.task,
  useAssistantResult: () => ({}),
  useRetryAssistantReport: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useAssistantArchive: () => ({ isPending: false, mutate: vi.fn() }),
}))
vi.mock("../api/assistant-watch", () => ({ useAssistantWatch: () => ({ data: { items: [], has_more: false } }) }))
vi.mock("../api/assistant-effects", () => ({ useAcknowledgeTaskEffects: () => ({ isPending: false, mutateAsync: api.acknowledge }) }))
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key, i18n: { language: "en-US" } }) }))
vi.mock("@/shared/hooks/useApiErrorMessage", () => ({ useApiErrorMessage: () => () => "Unavailable" }))
vi.mock("@/shared/ui/Toast", () => ({ toast: vi.fn() }))

const value: AssistantTaskView = {
  task: { id: "task-effects", title: "Deploy the site", project_id: "project", execution_session_id: "execution",
    desired_state: "paused", observed_state: "effect_unknown", control_revision: 7, intent_revision: 2, updated_at: "now" },
  execution_session: { id: "execution", status: "idle" }, run_binding: null, latest_result: null, latest_submission: null,
  pending_requests_location: "execution_session",
}
const receipt = { command_id: "ack", task_id: "task-effects", action: "acknowledge_effects", state: "applied",
  acknowledged_effects: 1, acknowledged_tool_outcomes: 1, remaining_unresolved: false, task_revision: 8,
  desired_state: "paused", observed_state: "paused" }

function mount(task: Partial<AssistantTaskView["task"]> = {}) {
  return render(<AssistantEffectAcknowledge value={{ ...value, task: { ...value.task, ...task } }} />)
}
function confirm() {
  fireEvent.click(screen.getByRole("button", { name: "assistant.card.effectAck.action" }))
}

beforeEach(() => {
  vi.mocked(toast).mockClear()
  api.acknowledge.mockReset()
  api.control.mockReset()
  api.task.mockReset()
  useEffectAcknowledgeRequests.setState({ taskIds: new Set() })
})
afterEach(cleanup)

describe("unverified outcome acknowledgment", () => {
  it("appears only while outcomes are unverified on a task that can still continue", () => {
    mount({ observed_state: "paused" })
    expect(screen.queryByRole("button")).toBeNull()
    cleanup()
    // A stopped task must be confirmed before a new message may reopen it.
    mount({ desired_state: "canceled" })
    expect(screen.getByRole("button", { name: "assistant.card.effectAck.action" })).toBeTruthy()
    cleanup()
    mount()
    expect(screen.getByText("assistant.card.effectAck.hint")).toBeTruthy()
  })
  it("is offered after a refused resume even before the task view catches up", () => {
    requestEffectAcknowledge("task-effects")
    mount({ observed_state: "resume_blocked" })
    expect(screen.getByRole("button", { name: "assistant.card.effectAck.action" })).toBeTruthy()
  })
  it("confirms with the current revision, then resumes with the revision it left", async () => {
    api.acknowledge.mockResolvedValue(receipt)
    api.control.mockResolvedValue({ command_id: "resume", state: "accepted" })
    mount()
    confirm()
    await waitFor(() => expect(api.control).toHaveBeenCalledOnce())
    expect(api.acknowledge.mock.calls[0][0]).toMatchObject({ taskId: "task-effects", revision: 7 })
    expect(api.control.mock.calls[0][0]).toMatchObject({ taskId: "task-effects", action: "resume", revision: 8, run: null })
    expect(toast).toHaveBeenCalledWith("info", "assistant.card.controlDone.resume")
  })
  it("does not resume while other outcomes are still being checked", async () => {
    api.acknowledge.mockResolvedValue({ ...receipt, remaining_unresolved: true, observed_state: "effect_unknown" })
    mount()
    confirm()
    await waitFor(() => expect(toast).toHaveBeenCalledWith("info", "assistant.card.effectAck.pending"))
    expect(api.control).not.toHaveBeenCalled()
  })
  it("only confirms for a running task, whose continuation was what waited", async () => {
    api.acknowledge.mockResolvedValue({ ...receipt, desired_state: "running", observed_state: "running" })
    requestEffectAcknowledge("task-effects")
    mount({ desired_state: "running", observed_state: "running" })
    confirm()
    await waitFor(() => expect(toast).toHaveBeenCalledWith("info", "assistant.card.effectAck.done"))
    expect(api.control).not.toHaveBeenCalled()
    expect(useEffectAcknowledgeRequests.getState().taskIds.has("task-effects")).toBe(false)
  })
  it("reports a refusal and keeps the same key for a retry of the same revision", async () => {
    api.acknowledge.mockRejectedValueOnce(new TypeError("timeout"))
      .mockRejectedValueOnce(new ApiError(409, "ASSISTANT_STILL_STOPPING", "running"))
    mount()
    confirm()
    await waitFor(() => expect(toast).toHaveBeenCalledWith("error", "Unavailable"))
    confirm()
    await waitFor(() => expect(api.acknowledge).toHaveBeenCalledTimes(2))
    expect(api.acknowledge.mock.calls[0][0].key).toBe(api.acknowledge.mock.calls[1][0].key)
    expect(api.control).not.toHaveBeenCalled()
  })
})

describe("task card", () => {
  it("offers the acknowledgment when a resume is refused for unverified outcomes", async () => {
    const blocked = { ...value, task: { ...value.task, observed_state: "resume_blocked" } }
    api.task.mockReturnValue({ data: blocked, error: null })
    api.control.mockRejectedValueOnce(new ApiError(409, "ASSISTANT_EFFECT_UNRESOLVED", "Verify outcomes"))
      .mockResolvedValueOnce({ command_id: "resume", state: "accepted" })
    api.acknowledge.mockResolvedValue(receipt)
    render(<MemoryRouter><AssistantTaskCard taskId="task-effects" /></MemoryRouter>)
    expect(screen.queryByRole("button", { name: "assistant.card.effectAck.action" })).toBeNull()
    fireEvent.click(screen.getByRole("button", { name: "assistant.card.more" }))
    fireEvent.click(screen.getByRole("menuitem", { name: "assistant.card.resume" }))
    await waitFor(() => expect(toast).toHaveBeenCalledWith("info", "assistant.card.checkEffects"))
    confirm()
    await waitFor(() => expect(toast).toHaveBeenCalledWith("info", "assistant.card.controlDone.resume"))
    expect(api.acknowledge.mock.calls[0][0]).toMatchObject({ taskId: "task-effects", revision: 7 })
    expect(api.control.mock.calls[1][0]).toMatchObject({ action: "resume", revision: 8 })
  })
})
