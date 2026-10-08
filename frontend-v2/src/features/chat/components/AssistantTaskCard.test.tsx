import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import type { AssistantTaskView } from "../api/assistant"
import type { AssistantWatchItem } from "../api/assistant-watch"
import type { ToolPart } from "@/shared/types/api"
import { toast } from "@/shared/ui/Toast"
import { taskReceipt } from "../lib/task-receipt"
import { AssistantTaskCard, AssistantTaskReceipts } from "./AssistantTaskCard"

const api = vi.hoisted(() => ({ task: vi.fn(), retry: vi.fn(), report: vi.fn(), archive: vi.fn(), control: vi.fn(), watch: vi.fn() }))
vi.mock("../api/assistant", () => ({
  useAssistantTask: api.task,
  useRetryAssistantReport: () => ({ isPending: false, mutateAsync: api.retry }),
  useAssistantResult: api.report,
  useAssistantControl: () => ({ isPending: false, mutateAsync: api.control }),
  useAssistantArchive: api.archive,
}))
vi.mock("../api/assistant-watch", () => ({ useAssistantWatch: api.watch }))
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key, i18n: { language: "en-US" } }) }))
vi.mock("@/shared/hooks/useApiErrorMessage", () => ({ useApiErrorMessage: () => () => "Unavailable" }))
vi.mock("@/shared/ui/Toast", () => ({ toast: vi.fn() }))

const value: AssistantTaskView = {
  task: { id: "task-01M48Y8NP3008", title: "Prepare the report", project_id: "project", execution_session_id: "execution",
    desired_state: "running", observed_state: "idle", control_revision: 2, intent_revision: 2, updated_at: new Date().toISOString() },
  execution_session: { id: "execution", status: "idle" }, run_binding: null,
  latest_submission: { submission_id: "submission", command_id: "command-immutable", inbox_id: "input", disposition: "accepted",
    accepted_at: "now", applied_at: null, run_id: null, generation: null },
  latest_result: { result_id: "result-7", run_id: "run", generation: 1, result_message_id: "original", outcome: "error",
    delivery_state: "blocked", report_attempt: 3, assistant_inbox_id: "report-inbox", processed_message_id: null,
    last_error_code: "source_read_failed", observed_intent_revision: 1, created_at: "now" },
  pending_requests_location: "execution_session",
}
const watched: AssistantWatchItem = {
  task_id: "task-01M48Y8NP3008", title: "Prepare the report", project: { id: "project", name: "Snake game" },
  session_id: "execution", session_status: "idle", desired_state: "running", observed_state: "idle", revision: 2,
  updated_at: "now", pending_questions: 0,
  latest_result: { result_id: "result-7", outcome: "error", delivery_state: "blocked", created_at: "now", summary: "The build failed on step 3." },
}

beforeEach(() => {
  vi.mocked(toast).mockClear()
  api.archive.mockReturnValue({ isPending: false, mutate: vi.fn() })
  api.task.mockReturnValue({ data: value, error: null })
  api.watch.mockReturnValue({ data: { items: [watched], has_more: false } })
  api.retry.mockReset()
  api.control.mockReset()
  api.report.mockReturnValue({ data: { pages: [{ offset: 0, sources: [{ part_id: "part", session_id: "execution", text: "Original failure report" }] }] } })
})
afterEach(cleanup)

function mount(props: Partial<Parameters<typeof AssistantTaskCard>[0]> = {}) {
  return render(<MemoryRouter><AssistantTaskCard taskId="task-01M48Y8NP3008" {...props} /></MemoryRouter>)
}
function openMenu() {
  fireEvent.click(screen.getByRole("button", { name: "assistant.card.more" }))
}

describe("assistant task card", () => {
  it("says what the task is, where it runs and the latest word, without any ids", () => {
    const { container } = mount()
    expect(screen.getByText("Prepare the report")).toBeTruthy()
    expect(screen.getByText("assistant.status.failed")).toBeTruthy()
    expect(screen.getByText(/Snake game/)).toBeTruthy()
    expect(screen.getByText("The build failed on step 3.")).toBeTruthy()
    expect(screen.getByRole("link", { name: /assistant.card.open/ }).getAttribute("href")).toBe("/app/s/execution")
    for (const id of ["task-01M48Y8NP3008", "command-immutable", "result-7", "report-inbox", "run"])
      expect(container.textContent).not.toContain(id)
  })
  it("asks for the user's reply first when the conversation waits on them", () => {
    api.watch.mockReturnValue({ data: { items: [{ ...watched, pending_questions: 1 }], has_more: false } })
    mount()
    expect(screen.getByText("assistant.status.waiting")).toBeTruthy()
    expect(screen.getByText("assistant.card.waitingHint")).toBeTruthy()
    expect(screen.getByRole("link", { name: /assistant.card.answer/ })).toBeTruthy()
  })
  it("still renders a task the watch list no longer carries", () => {
    api.watch.mockReturnValue({ data: { items: [], has_more: false } })
    api.task.mockReturnValue({ data: { ...value, latest_result: { ...value.latest_result!, outcome: "succeeded", delivery_state: "processed" } } })
    mount()
    expect(screen.getByText("assistant.status.done")).toBeTruthy()
    expect(screen.queryByText("The build failed on step 3.")).toBeNull()
  })
  it.each([
    ["active", "assistant.card.followingUp"],
    ["needs_decision", "assistant.card.continuation.needs_decision"],
    ["exhausted", "assistant.card.continuation.exhausted"],
  ])("explains continued work in state %s", (state, key) => {
    api.task.mockReturnValue({ data: { ...value, task: { ...value.task,
      continuation: { state, followups_used: 1, max_followups: 2, expires_at: null, last_result_id: null, reason: null } } } })
    mount()
    expect(screen.getByText(key)).toBeTruthy()
  })
  it.each(["completed", "revoked"])("says nothing extra once continued work is %s", (state) => {
    api.task.mockReturnValue({ data: { ...value, latest_result: null, task: { ...value.task,
      continuation: { state, followups_used: 2, max_followups: 2, expires_at: null, last_result_id: null, reason: null } } } })
    mount()
    expect(screen.queryByText(/assistant.card.continuation/)).toBeNull()
    expect(screen.queryByText("assistant.card.followingUp")).toBeNull()
  })
  it.each([
    [{ disposition: "canceled", state: "canceled", error: { code: "ASSISTANT_ASSET_UNAVAILABLE" } }, "assistant.card.note.assetUnavailable"],
    [{ disposition: "canceled", state: "canceled" }, "assistant.card.note.inputCanceled"],
    [{ delivery: "steer", disposition: "not_applied", state: "canceled" }, "assistant.card.note.steerNotApplied"],
  ])("explains a submission that did not run (%o)", (submission, key) => {
    api.task.mockReturnValue({ data: { ...value, latest_result: null,
      latest_submission: { ...value.latest_submission!, ...submission } } })
    mount()
    expect(screen.getByText(key)).toBeTruthy()
  })
  it("explains a blocked report and an unknown outcome in plain words", () => {
    mount()
    expect(screen.getByText("assistant.card.note.reportFailed")).toBeTruthy()
    cleanup()
    api.task.mockReturnValue({ data: { ...value, latest_result: { ...value.latest_result!, last_error_code: "user_stopped" } } })
    mount()
    expect(screen.getByText("assistant.card.note.reportStopped")).toBeTruthy()
    cleanup()
    api.task.mockReturnValue({ data: { ...value, task: { ...value.task, observed_state: "effect_unknown" } } })
    mount()
    expect(screen.getByText("assistant.card.note.effectUnknown")).toBeTruthy()
  })
  it("shows the full result on request, with a loading line only before the first page", () => {
    api.report.mockReturnValue({ isPending: true })
    mount()
    openMenu()
    fireEvent.click(screen.getByRole("menuitem", { name: "assistant.card.showResult" }))
    expect(screen.getByText("assistant.card.loadingResult")).toBeTruthy()
    cleanup()
    api.report.mockReturnValue({ isPending: false, isFetching: true,
      data: { pages: [{ offset: 0, sources: [{ part_id: "part", session_id: "execution", text: "Original failure report" }] }] } })
    mount()
    openMenu()
    fireEvent.click(screen.getByRole("menuitem", { name: "assistant.card.showResult" }))
    expect(screen.getByText("Original failure report")).toBeTruthy()
  })
  it("retries only the report with one durable key after a lost response", async () => {
    api.retry.mockRejectedValueOnce(new TypeError("timeout")).mockResolvedValueOnce({ state: "accepted" })
    mount()
    expect(api.retry).not.toHaveBeenCalled()
    openMenu()
    fireEvent.click(screen.getByRole("menuitem", { name: "assistant.card.retryReport" }))
    await waitFor(() => expect(api.retry).toHaveBeenCalledTimes(1))
    openMenu()
    fireEvent.click(screen.getByRole("menuitem", { name: "assistant.card.retryReport" }))
    await waitFor(() => expect(api.retry).toHaveBeenCalledTimes(2))
    expect(api.retry.mock.calls[0][0]).toEqual(api.retry.mock.calls[1][0])
    expect(api.retry.mock.calls[0][0]).toMatchObject({ resultId: "result-7", attempt: 3 })
  })
  it("offers retrying a report only while it is blocked", () => {
    api.task.mockReturnValue({ data: { ...value, latest_result: { ...value.latest_result!, delivery_state: "processed" } } })
    mount()
    openMenu()
    expect(screen.queryByRole("menuitem", { name: "assistant.card.retryReport" })).toBeNull()
  })
  it("pauses with the task's current revision and run, and only offers resume once paused", async () => {
    api.control.mockResolvedValue({ command_id: "receipt" })
    api.task.mockReturnValue({ data: { ...value, run_binding: { run_id: "live-run", generation: 4, phase: "running" } } })
    mount()
    openMenu()
    expect(screen.queryByRole("menuitem", { name: "assistant.card.resume" })).toBeNull()
    fireEvent.click(screen.getByRole("menuitem", { name: "assistant.card.pause" }))
    await waitFor(() => expect(api.control).toHaveBeenCalledOnce())
    expect(api.control.mock.calls[0][0]).toMatchObject({ taskId: "task-01M48Y8NP3008", action: "pause", revision: 2,
      run: { run_id: "live-run", generation: 4 } })
    await waitFor(() => expect(toast).toHaveBeenCalledWith("info", "assistant.card.controlDone.pause"))
    cleanup()
    api.task.mockReturnValue({ data: { ...value, task: { ...value.task, desired_state: "paused", observed_state: "paused" } } })
    mount()
    openMenu()
    expect(screen.queryByRole("menuitem", { name: "assistant.card.pause" })).toBeNull()
    expect(screen.getByRole("menuitem", { name: "assistant.card.resume" })).toBeTruthy()
  })
  it("stops following with the current revision, and reports a refusal", () => {
    const mutate = vi.fn((_vars, options: { onSuccess: (receipt: unknown) => void }) => options.onSuccess({ state: "archived" }))
    api.archive.mockReturnValue({ isPending: false, mutate })
    mount()
    openMenu()
    fireEvent.click(screen.getByRole("menuitem", { name: "assistant.card.unfollow" }))
    expect(mutate).toHaveBeenCalledExactlyOnceWith({ taskId: "task-01M48Y8NP3008", revision: 2 }, expect.anything())
    expect(toast).toHaveBeenCalledWith("info", "assistant.card.unfollowed")
    cleanup()
    const refused = vi.fn((_vars, options: { onError: (error: Error) => void }) => options.onError(new Error("refused")))
    api.archive.mockReturnValue({ isPending: false, mutate: refused })
    mount()
    openMenu()
    fireEvent.click(screen.getByRole("menuitem", { name: "assistant.card.unfollow" }))
    expect(toast).toHaveBeenCalledWith("error", "Unavailable")
  })
  it("offers no controls for a task no longer followed", () => {
    api.task.mockReturnValue({ data: { ...value, task: { ...value.task, archived_at: "2026-10-06T00:00:00Z" } } })
    mount()
    openMenu()
    for (const key of ["unfollow", "pause", "cancel"])
      expect(screen.queryByRole("menuitem", { name: `assistant.card.${key}` })).toBeNull()
  })
  it("says plainly that a deleted task's conversation is gone instead of raising an error", async () => {
    const { ApiError } = await import("@/shared/api/http")
    api.task.mockReturnValue({ data: undefined, error: new ApiError(409, "ASSISTANT_EXECUTION_UNAVAILABLE", "gone") })
    mount()
    expect(screen.getByText("assistant.card.gone")).toBeTruthy()
    expect(screen.queryByRole("alert")).toBeNull()
    cleanup()
    api.task.mockReturnValue({ data: undefined, error: new ApiError(500, "HTTP_500", "boom") })
    mount()
    expect(screen.getByRole("alert").textContent).toBe("Unavailable")
  })

  it("uses the refreshed list snapshot over an older failed detail query", () => {
    const fresh = { ...value, latest_result: { ...value.latest_result!, delivery_state: "processed" as const, outcome: "succeeded" } }
    api.task.mockReturnValue({ data: value, error: new Error("Older detail failed") })
    mount({ initial: fresh })
    expect(screen.getByText("assistant.status.done")).toBeTruthy()
    expect(screen.queryByRole("alert")).toBeNull()
  })
  it("renders one card per task even when a turn changed it twice", () => {
    const part = (tool: string, command: string) => ({ id: command, type: "tool", tool, status: "completed",
      output: JSON.stringify({ task_id: "task-01M48Y8NP3008", command_id: command, state: "accepted" }) }) as unknown as ToolPart
    render(<MemoryRouter><AssistantTaskReceipts parts={[part("tasks.submit", "a"), part("tasks.followup", "b")]} /></MemoryRouter>)
    expect(screen.getAllByText("Prepare the report")).toHaveLength(1)
  })
})

describe("assistant task receipts", () => {
  it.each([["continue", "accepted"], ["complete", "completed"], ["needs_decision", "needs_decision"]])("recognizes only the matching continuation %s receipt", (decision, state) => {
    const part = { tool: "tasks.next_step", status: "completed", output: JSON.stringify({
      task_id: "t", command_id: "c", decision, state }) } as ToolPart
    expect(taskReceipt(part)).toEqual({ taskId: "t", commandId: "c" })
    expect(taskReceipt({ ...part, output: JSON.stringify({ task_id: "t", command_id: "c", state }) })).toBeNull()
  })
  it("requires a canonical completed write tool receipt rather than IDs found in prose", () => {
    const part = { tool: "tasks.submit", status: "completed", output: JSON.stringify({ task_id: "t", command_id: "c", state: "accepted" }) } as ToolPart
    expect(taskReceipt(part)).toEqual({ taskId: "t", commandId: "c" })
    expect(taskReceipt({ ...part, tool: "assets.attach" })).toEqual({ taskId: "t", commandId: "c" })
    expect(taskReceipt({ ...part, tool: "schedules.run" })).toEqual({ taskId: "t", commandId: "c" })
    expect(taskReceipt({ ...part, tool: "schedules.create" })).toBeNull()
    expect(taskReceipt({ ...part, tool: "assets.list" })).toBeNull()
    expect(taskReceipt({ ...part, tool: "read" })).toBeNull()
    expect(taskReceipt({ ...part, status: "running" })).toBeNull()
    expect(taskReceipt({ ...part, output: "Task t was accepted" })).toBeNull()
  })
})
