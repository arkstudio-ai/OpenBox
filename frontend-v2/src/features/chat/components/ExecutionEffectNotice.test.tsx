import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import type { Session } from "@/shared/types/api"
import { toast } from "@/shared/ui/Toast"
import { requestEffectAcknowledgeForSession, useEffectAcknowledgeRequests } from "../stores/effect-acknowledge"
import { ExecutionEffectNotice, StopTaskNotice } from "./ExecutionEffectNotice"

const api = vi.hoisted(() => ({ acknowledge: vi.fn() }))
vi.mock("../api/assistant-effects", () => ({ useAcknowledgeTaskEffects: () => ({ isPending: false, mutateAsync: api.acknowledge }) }))
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key, i18n: { language: "en-US" } }) }))
vi.mock("@/shared/hooks/useApiErrorMessage", () => ({ useApiErrorMessage: () => () => "Unavailable" }))
vi.mock("@/shared/ui/Toast", () => ({ toast: vi.fn() }))

const session = {
  id: "execution", user_id: "user", workspace_id: "workspace", title: "艾薇兰小红瓶", status: "idle",
  created_at: "now", updated_at: "now", assistant_managed: true, kind: "normal",
  task_control: { task_id: "task-1", expected_revision: 12, expected_run: null, desired_state: "canceled", observed_state: "effect_unknown" },
} as unknown as Session

function mount(value: Session = session) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <ExecutionEffectNotice sessionId="execution" session={value} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.mocked(toast).mockClear()
  api.acknowledge.mockReset()
  useEffectAcknowledgeRequests.setState({ taskIds: new Set(), sessionIds: new Set() })
})
afterEach(cleanup)

describe("execution page effect acknowledgment", () => {
  it("shows only on a task-linked page whose task waits for confirmation, or after a refused send", () => {
    mount({ ...session, assistant_managed: false, task_control: null })
    expect(screen.queryByRole("button")).toBeNull()
    cleanup()
    mount({ ...session, task_control: { ...session.task_control!, observed_state: "running" } })
    expect(screen.queryByRole("button")).toBeNull()
    cleanup()
    requestEffectAcknowledgeForSession("execution")
    mount({ ...session, task_control: { ...session.task_control!, observed_state: "running" } })
    expect(screen.getByRole("button", { name: "execution.effectAck.action" })).toBeTruthy()
    cleanup()
    mount()
    expect(screen.getByText("execution.effectAck.title")).toBeTruthy()
  })

  it("confirms with the task's current revision and clears the page's request", async () => {
    requestEffectAcknowledgeForSession("execution")
    api.acknowledge.mockResolvedValue({ remaining_unresolved: false, task_revision: 13 })
    mount()
    fireEvent.click(screen.getByRole("button", { name: "execution.effectAck.action" }))
    await waitFor(() => expect(api.acknowledge).toHaveBeenCalledTimes(1))
    expect(api.acknowledge.mock.calls[0][0]).toMatchObject({ taskId: "task-1", revision: 12 })
    await waitFor(() => expect(toast).toHaveBeenCalledWith("info", "execution.effectAck.done"))
    expect(useEffectAcknowledgeRequests.getState().sessionIds.has("execution")).toBe(false)
  })

  it("says the task is still blocked when outcomes remain unresolved", async () => {
    api.acknowledge.mockResolvedValue({ remaining_unresolved: true, task_revision: 13 })
    mount()
    fireEvent.click(screen.getByRole("button", { name: "execution.effectAck.action" }))
    await waitFor(() => expect(toast).toHaveBeenCalledWith("info", "execution.effectAck.pending"))
  })
})

describe("stop task confirmation", () => {
  it("offers stop and keep, each wired to its callback", () => {
    const onConfirm = vi.fn()
    const onKeep = vi.fn()
    render(<StopTaskNotice open={false} pending={false} onConfirm={onConfirm} onKeep={onKeep} />)
    expect(screen.queryByRole("button")).toBeNull()
    cleanup()
    render(<StopTaskNotice open pending={false} onConfirm={onConfirm} onKeep={onKeep} />)
    fireEvent.click(screen.getByRole("button", { name: "execution.stopTask.keep" }))
    expect(onKeep).toHaveBeenCalledTimes(1)
    expect(onConfirm).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole("button", { name: "execution.stopTask.confirm" }))
    expect(onConfirm).toHaveBeenCalledTimes(1)
  })
})
