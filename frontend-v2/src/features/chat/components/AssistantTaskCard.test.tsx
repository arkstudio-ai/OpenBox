import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import type { AssistantSnapshot, AssistantTaskView } from "../api/assistant"
import type { ToolPart } from "@/shared/types/api"
import { toast } from "@/shared/ui/Toast"
import { AssistantReadContext } from "../hooks/assistant-read-context"
import { taskReceipt } from "../lib/task-receipt"
import { AssistantTaskCard } from "./AssistantTaskCard"

const api = vi.hoisted(() => ({ task: vi.fn(), retry: vi.fn(), report: vi.fn(), archive: vi.fn() }))
vi.mock("../api/assistant", () => ({
  useAssistantTask: api.task,
  useRetryAssistantReport: () => ({ isPending: false, mutateAsync: api.retry }),
  useAssistantResult: api.report,
  useAssistantControl: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useAssistantArchive: api.archive,
}))
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
vi.mock("@/shared/hooks/useApiErrorMessage", () => ({ useApiErrorMessage: () => () => "Unavailable" }))
vi.mock("@/shared/ui/Toast", () => ({ toast: vi.fn() }))

const value: AssistantTaskView = {
  task: { id: "task", title: "Prepare the report", project_id: "project", execution_session_id: "execution",
    desired_state: "running", observed_state: "idle", control_revision: 2, intent_revision: 2, updated_at: "now" },
  execution_session: { id: "execution", status: "idle" }, run_binding: null,
  latest_submission: { submission_id: "submission", command_id: "command", inbox_id: "input", disposition: "accepted",
    accepted_at: "now", applied_at: null, run_id: null, generation: null },
  latest_result: { result_id: "result", run_id: "run", generation: 1, result_message_id: "original", outcome: "error",
    delivery_state: "blocked", report_attempt: 3, assistant_inbox_id: "report-inbox", processed_message_id: null,
    last_error_code: "source_read_failed", observed_intent_revision: 1, created_at: "now" },
  pending_requests_location: "execution_session",
}

beforeEach(() => {
  vi.mocked(toast).mockClear()
  api.archive.mockReturnValue({ isPending: false, mutate: vi.fn() })
  api.task.mockReturnValue({ data: value, error: null })
  api.retry.mockReset()
  api.report.mockReturnValue({ data: { pages: [{ offset: 0, sources: [{ part_id: "part", session_id: "execution", text: "Original failure report" }] }] } })
})
afterEach(cleanup)

function mount(cursor = 0) {
  return render(<MemoryRouter><AssistantReadContext.Provider value={{ snapshot: { answers: [], last_seen_sequence: cursor } as unknown as AssistantSnapshot,
    displayed: vi.fn() }}><AssistantTaskCard taskId="task" commandId="immutable-command" /></AssistantReadContext.Provider></MemoryRouter>)
}

describe("assistant task receipts", () => {
  it.each(["active", "completed", "needs_decision", "exhausted", "revoked"])("shows continued-work %s independently of a finished execution", (state) => {
    api.task.mockReturnValue({ data: { ...value, task: { ...value.task, observed_state: "completed",
      continuation: { state, followups_used: 1, max_followups: 2 } } } })
    mount()
    expect(screen.getByText(new RegExp(`assistant.continuation.${state}`))).toBeTruthy()
  })
  it.each([["continue", "accepted"], ["complete", "completed"], ["needs_decision", "needs_decision"]])("recognizes only the matching continuation %s receipt", (decision, state) => {
    const part = { tool: "tasks.next_step", status: "completed", output: JSON.stringify({
      task_id: "t", command_id: "c", decision, state }) } as ToolPart
    expect(taskReceipt(part)).toEqual({ taskId: "t", commandId: "c" })
    expect(taskReceipt({ ...part, output: JSON.stringify({ task_id: "t", command_id: "c", state }) })).toBeNull()
  })
  it("explains unavailable attachments without claiming that canceled input is still queued", () => {
    api.task.mockReturnValue({ data: { ...value, latest_submission: { ...value.latest_submission,
      disposition: "canceled", state: "canceled", error: { code: "ASSISTANT_ASSET_UNAVAILABLE" } } } })
    mount()
    expect(screen.getByText("assistant.inputAssetUnavailable")).toBeTruthy()
    expect(screen.queryByText("assistant.inputAccepted")).toBeNull()
    expect(screen.queryByText("assistant.inputApplied")).toBeNull()
  })
  it.each([
    ["accepted", null, "accepted", "steerAccepted"],
    ["applied", "now", "settled", "steerApplied"],
    ["not_applied", null, "canceled", "steerNotApplied"],
  ])("separates steering %s from execution outcome", (disposition, appliedAt, state, label) => {
    api.task.mockReturnValue({ data: { ...value, latest_submission: { ...value.latest_submission,
      delivery: "steer", disposition, applied_at: appliedAt, state } } })
    mount()
    expect(screen.getByText(`assistant.${label}`)).toBeTruthy()
    expect(screen.getByText("assistant.executionFailed")).toBeTruthy()
    expect(screen.queryByText("assistant.inputAccepted")).toBeNull()
  })
  it("shows separate execution/acceptance/report/read facts and identifies a previous result", async () => {
    mount()
    for (const key of ["executionFailed", "accepted", "reportBlocked", "noAnswer", "inputAccepted", "earlierResult"])
      expect(screen.getByText(`assistant.${key}`)).toBeTruthy()
    expect(screen.queryByText("assistant.processed")).toBeNull()
    expect(screen.queryByText("assistant.read")).toBeNull()
    expect(screen.getByRole("link", { name: "assistant.openTask" }).getAttribute("href")).toBe("/app/s/execution")
    fireEvent.click(screen.getByText("assistant.originalReport"))
    expect(screen.getByText("Original failure report")).toBeTruthy()
  })
  it("shows the loaded original report while it refreshes in the background", () => {
    api.report.mockReturnValue({ isPending: false, isFetching: true, isFetchingNextPage: false,
      data: { pages: [{ offset: 0, sources: [{ part_id: "part", session_id: "execution", text: "Original failure report" }] }] } })
    mount()
    fireEvent.click(screen.getByText("assistant.originalReport"))
    expect(screen.getByText("Original failure report")).toBeTruthy()
    expect(screen.queryByText("assistant.loadingReport")).toBeNull()
  })
  it("shows a plain loading line only before the report's first page arrives", () => {
    api.report.mockReturnValue({ isPending: true, isFetching: true })
    mount()
    fireEvent.click(screen.getByText("assistant.originalReport"))
    expect(screen.getByText("assistant.loadingReport")).toBeTruthy()
  })
  it("stops watching with the task's current revision and says so once confirmed", () => {
    const mutate = vi.fn((_vars, options: { onSuccess: (receipt: unknown) => void }) => options.onSuccess({ state: "archived" }))
    api.archive.mockReturnValue({ isPending: false, mutate })
    mount()
    fireEvent.click(screen.getByRole("button", { name: "assistant.watch.archive" }))
    expect(mutate).toHaveBeenCalledExactlyOnceWith({ taskId: "task", revision: 2 }, expect.anything())
    expect(screen.queryByRole("button", { name: "assistant.watch.archive" })).toBeNull()
    expect(screen.getByText("assistant.watch.archived")).toBeTruthy()
    // The conversation itself stays reachable.
    expect(screen.getByRole("link", { name: "assistant.openTask" })).toBeTruthy()
  })
  it("disables stopping to watch while the request is pending", () => {
    api.archive.mockReturnValue({ isPending: true, mutate: vi.fn() })
    mount()
    const button = screen.getByRole("button", { name: "assistant.watch.archiving" }) as HTMLButtonElement
    expect(button.disabled).toBe(true)
  })
  it("reports a refused stop with the error toast and keeps the action", () => {
    const mutate = vi.fn((_vars, options: { onError: (error: Error) => void }) => options.onError(new Error("refused")))
    api.archive.mockReturnValue({ isPending: false, mutate })
    mount()
    fireEvent.click(screen.getByRole("button", { name: "assistant.watch.archive" }))
    expect(toast).toHaveBeenCalledWith("error", "Unavailable")
    expect(screen.getByRole("button", { name: "assistant.watch.archive" })).toBeTruthy()
  })
  it("shows a task the server reports as archived without offering to stop again", () => {
    api.task.mockReturnValue({ data: { ...value, task: { ...value.task, archived_at: "2026-10-06T00:00:00Z" } } })
    mount()
    expect(screen.getByText("assistant.watch.archived")).toBeTruthy()
    expect(screen.queryByRole("button", { name: "assistant.watch.archive" })).toBeNull()
  })
  it("does not equate a saved report with a user having read it", () => {
    api.task.mockReturnValue({ data: { ...value, latest_result: { ...value.latest_result,
      delivery_state: "processed", processed_sequence: 10, processed_message_id: "answer" } } })
    const view = mount(5)
    expect(screen.getByText("assistant.processed")).toBeTruthy()
    expect(screen.getByText("assistant.unread")).toBeTruthy()
    view.unmount()
    mount(12)
    expect(screen.getByText("assistant.read")).toBeTruthy()
  })
  it("uses the refreshed list snapshot over an older disabled detail query", () => {
    const fresh = { ...value, latest_result: { ...value.latest_result!, delivery_state: "processed" as const,
      outcome: "succeeded", processed_message_id: "answer", processed_sequence: 10 } }
    api.task.mockReturnValue({ data: value, error: new Error("Older detail failed") })
    render(<MemoryRouter><AssistantTaskCard taskId="task" initial={fresh} /></MemoryRouter>)
    expect(screen.getByText("assistant.processed")).toBeTruthy()
    expect(screen.getByText("assistant.executionSucceeded")).toBeTruthy()
    expect(screen.queryByRole("alert")).toBeNull()
  })
  it("retries only the report with one durable key after a lost response", async () => {
    api.retry.mockRejectedValueOnce(new TypeError("timeout")).mockResolvedValueOnce({ state: "accepted" })
    mount()
    expect(api.retry).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText("assistant.retryReport"))
    await waitFor(() => expect(api.retry).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByText("assistant.retryReport"))
    await waitFor(() => expect(api.retry).toHaveBeenCalledTimes(2))
    expect(api.retry.mock.calls[0][0]).toEqual(api.retry.mock.calls[1][0])
    expect(api.retry.mock.calls[0][0]).toMatchObject({ resultId: "result", attempt: 3 })
    expect(screen.getByText("immutable-command")).toBeTruthy()
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
