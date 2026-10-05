import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { toast } from "@/shared/ui/Toast"
import { useAssistantTask, type AssistantTaskView } from "../api/assistant"
import { AssistantTaskControls } from "./AssistantTaskControls"

vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
vi.mock("@/shared/hooks/useApiErrorMessage", () => ({ useApiErrorMessage: () => () => "Unavailable" }))
vi.mock("@/shared/ui/Toast", () => ({ toast: vi.fn() }))

const value = {
  task: { id: "controlled-task", title: "Test", execution_session_id: "original-session", project_id: "project",
    desired_state: "running", observed_state: "running", control_revision: 8, intent_revision: 2, updated_at: "now" },
  run_binding: { run_id: "observed-run", generation: 4, phase: "running" },
} as AssistantTaskView
let fetchMock: ReturnType<typeof vi.fn>
beforeEach(() => {
  fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ command_id: "control-receipt", state: "accepted" }), { status: 202 }))
  vi.stubGlobal("fetch", fetchMock)
  useAuthStore.setState({ user: { id: "test-owner" } as never, accessToken: null })
  useWorkspaceStore.setState({ currentId: "test-workspace" })
  vi.mocked(toast).mockClear()
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })
function mount(data = value) {
  const query = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  const ui = (current: AssistantTaskView) => <QueryClientProvider client={query}><AssistantTaskControls value={current} /></QueryClientProvider>
  const view = render(ui(data))
  return { ...view, update: (current: AssistantTaskView) => view.rerender(ui(current)) }
}

it("sends the displayed revision and exact run through the real API adapter without synthetic input", async () => {
  mount()
  fireEvent.click(screen.getByRole("button", { name: "assistant.task.control.pause" }))
  await screen.findByText("control-receipt")
  const [url, request] = fetchMock.mock.calls[0]
  expect(url).toContain("/api/assistant/tasks/controlled-task/commands")
  expect(new Headers(request.headers).get("X-Workspace-Id")).toBe("test-workspace")
  expect(JSON.parse(request.body)).toEqual({ action: "pause", expected_revision: 8,
    expected_run: { run_id: "observed-run", generation: 4 }, idempotency_key: expect.any(String) })
  expect(screen.getByText(/assistant.task.state.running/)).toBeTruthy()
  expect(screen.queryByText(/assistant.task.state.paused/)).toBeNull()
})

it("retries a lost response with the same command key and prevents duplicate clicks", async () => {
  fetchMock.mockRejectedValueOnce(new TypeError("lost response"))
  mount()
  const button = screen.getByRole("button", { name: "assistant.task.control.pause" })
  fireEvent.click(button)
  fireEvent.click(button)
  await waitFor(() => expect(toast).toHaveBeenCalledWith("error", "Unavailable"))
  expect(fetchMock).toHaveBeenCalledTimes(1)
  fireEvent.click(button)
  await screen.findByText("control-receipt")
  expect(fetchMock.mock.calls[0][1].body).toEqual(fetchMock.mock.calls[1][1].body)
})

it("reports stale state without automatically applying the action to a newer revision", async () => {
  fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ detail: { code: "ASSISTANT_REVISION_CONFLICT", message: "Changed" } }), { status: 409 }))
  mount()
  fireEvent.click(screen.getByRole("button", { name: "assistant.task.control.cancel" }))
  await waitFor(() => expect(toast).toHaveBeenCalledWith("info", "assistant.task.changed"))
  expect(fetchMock).toHaveBeenCalledTimes(1)
  expect(screen.queryByText("control-receipt")).toBeNull()
})

it("reloads and displays the latest task after another device wins, then requires a fresh click", async () => {
  const latest = { ...value, task: { ...value.task, desired_state: "paused", observed_state: "paused", control_revision: 9 },
    run_binding: { run_id: null, generation: 4, phase: "idle" } } as AssistantTaskView
  let current = value
  const commands: Record<string, unknown>[] = []
  fetchMock.mockImplementation(async (_url: string, request: RequestInit) => {
    if (request.method === "POST") {
      commands.push(JSON.parse(request.body as string))
      if (commands.length === 1) {
        current = latest
        return new Response(JSON.stringify({ detail: { code: "ASSISTANT_REVISION_CONFLICT", message: "Changed",
          current_task: latest.task } }), { status: 409 })
      }
      return new Response(JSON.stringify({ command_id: "fresh-resume", state: "accepted" }), { status: 202 })
    }
    return new Response(JSON.stringify(current))
  })
  function LoadedControls() {
    const task = useAssistantTask(value.task.id)
    return task.data ? <AssistantTaskControls value={task.data} /> : null
  }
  const query = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(<QueryClientProvider client={query}><LoadedControls /></QueryClientProvider>)
  fireEvent.click(await screen.findByRole("button", { name: "assistant.task.control.pause" }))
  await screen.findByText(/assistant.task.state.paused/)
  expect(toast).toHaveBeenCalledWith("info", "assistant.task.changed")
  expect(commands).toHaveLength(1)
  expect(screen.queryByRole("button", { name: "assistant.task.control.pause" })).toBeNull()
  expect(commands[0]).toMatchObject({ action: "pause", expected_revision: 8,
    expected_run: { run_id: "observed-run", generation: 4 } })
  fireEvent.click(screen.getByRole("button", { name: "assistant.task.control.resume" }))
  await screen.findByText("fresh-resume")
  expect(commands).toHaveLength(2)
  expect(commands[1]).toMatchObject({ action: "resume", expected_revision: 9, expected_run: null })
  expect(commands[1].idempotency_key).not.toBe(commands[0].idempotency_key)
})

it.each(["pausing", "effect_unknown"])("does not offer an executable resume while %s", (state) => {
  mount({ ...value, task: { ...value.task, desired_state: "paused", observed_state: state } })
  const resume = screen.getByRole("button", { name: "assistant.task.control.resume" }) as HTMLButtonElement
  expect(resume.disabled).toBe(true)
  fireEvent.click(resume)
  expect(fetchMock).not.toHaveBeenCalled()
})

it("resumes an idle paused task with no old run identity and leaves completion to SQL", async () => {
  mount({ ...value, task: { ...value.task, desired_state: "paused", observed_state: "paused" },
    run_binding: { run_id: null, generation: 4, phase: "idle" } })
  fireEvent.click(screen.getByRole("button", { name: "assistant.task.control.resume" }))
  await screen.findByText("control-receipt")
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toMatchObject({ action: "resume", expected_run: null })
  expect(screen.getByText(/assistant.task.state.paused/)).toBeTruthy()
})

it("shows canceled tasks without actions that could resurrect them", () => {
  mount({ ...value, task: { ...value.task, desired_state: "canceled", observed_state: "canceled" } })
  expect(screen.queryByRole("button")).toBeNull()
})

it("shows the new accepted receipt while a snapshot lags, then follows newer server controls", async () => {
  const previous = { command_id: "old-receipt", action: "task_resume", state: "applied", receipt: { task_revision: 7 } }
  const view = mount({ ...value, latest_control: previous })
  fireEvent.click(screen.getByRole("button", { name: "assistant.task.control.pause" }))
  await screen.findByText("control-receipt")
  expect(screen.queryByText("old-receipt")).toBeNull()
  view.update({ ...value, latest_control: { ...previous, command_id: "newer-server-receipt", receipt: { task_revision: 12 } } })
  expect(screen.getByText("newer-server-receipt")).toBeTruthy()
  expect(screen.queryByText("control-receipt")).toBeNull()
  view.update({ ...value, task: { ...value.task, id: "another-task" } })
  expect(screen.queryByText("control-receipt")).toBeNull()
})
