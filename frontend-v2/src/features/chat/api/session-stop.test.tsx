// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from "vitest"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, renderHook } from "@testing-library/react"
import type { PropsWithChildren } from "react"
import { ApiError, http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import type { Session } from "@/shared/types/api"
import { usePendingStore } from "../stores/pending"
import { stopSession, useAbortSession } from "./messages"

vi.mock("@/shared/api/http", async (original) => ({
  ...(await original<typeof import("@/shared/api/http")>()), http: { post: vi.fn(), get: vi.fn() },
}))

let counter = 0
function session(): Session {
  return { id: `stop-${++counter}`, user_id: "stop-owner", workspace_id: "stop-workspace",
    assistant_managed: true, kind: "normal", title: "Task", agent: "build", model: "test/model",
    created_at: "2026-10-04", updated_at: "2026-10-04", status: "busy",
    task_control: { task_id: "original-task", expected_revision: 2,
      expected_run: { run_id: "original-run", generation: 3 }, desired_state: "running", observed_state: "running" } }
}
beforeEach(() => {
  vi.resetAllMocks()
  useAuthStore.setState({ user: { id: "stop-owner" } as NonNullable<ReturnType<typeof useAuthStore.getState>["user"]> })
  useWorkspaceStore.setState({ currentId: "stop-workspace" })
})

it.each([new Error("response lost"), new ApiError(408, "TIMEOUT", "timeout"), new ApiError(429, "QUOTA", "retry")])(
  "replays an uncertain stop against the original run after polling changes the target", async (error) => {
    const value = session()
    vi.mocked(http.post).mockRejectedValueOnce(error).mockResolvedValue({ ok: true, task_control: { command_id: "original-stop" } })
    await expect(stopSession(value.id, value)).rejects.toBe(error)
    const oldBody = vi.mocked(http.post).mock.calls[0][1]
    value.task_control = { ...value.task_control!, expected_revision: 8, expected_run: { run_id: "new-run", generation: 4 } }
    await stopSession(value.id, value)
    expect(vi.mocked(http.post).mock.calls[1][1]).toEqual(oldBody)
    expect(vi.mocked(http.post).mock.calls[1][2]).toEqual({ headers: { "X-Workspace-Id": "stop-workspace" } })
    await stopSession(value.id, value)
    expect(vi.mocked(http.post).mock.calls[2][1]).toMatchObject({ task_control: { expected_revision: 8 } })
  })

it("requires a new observed target after a definitive stale-stop refusal", async () => {
  const value = session()
  vi.mocked(http.post).mockRejectedValueOnce(new ApiError(409, "ASSISTANT_RUN_CONFLICT", "changed"))
    .mockResolvedValue({ ok: true, task_control: { command_id: "next-stop" } })
  await expect(stopSession(value.id, value)).rejects.toBeInstanceOf(ApiError)
  value.task_control = { ...value.task_control!, expected_revision: 9 }
  await stopSession(value.id, value)
  expect(vi.mocked(http.post).mock.calls[1][1]).toMatchObject({ task_control: { expected_revision: 9 } })
})

it.each(["no-target", "actor", "workspace"])("does not fall back to a raw stop for %s", async (change) => {
  const value = session()
  if (change === "no-target") value.task_control = null
  if (change === "actor") useAuthStore.setState({ user: null })
  if (change === "workspace") useWorkspaceStore.setState({ currentId: "other-workspace" })
  await expect(stopSession(value.id, value)).rejects.toBeInstanceOf(ApiError)
  expect(http.post).not.toHaveBeenCalled()
})

it("keeps ordinary conversation stop unchanged", async () => {
  vi.mocked(http.post).mockResolvedValue({ ok: true })
  const value = { ...session(), assistant_managed: false }
  await stopSession(value.id, value)
  expect(http.post).toHaveBeenCalledWith(`/api/agent/session/${value.id}/abort`)
})

it("does not hide a pending question merely because cancellation was accepted", async () => {
  const value = session()
  vi.mocked(http.post).mockResolvedValue({ ok: true, task_control: { command_id: "accepted-stop" } })
  const question = { id: "waiting-question", session_id: value.id, questions: [] }
  usePendingStore.setState({ questions: new Map([[value.id, [question]]]) })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: PropsWithChildren) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
  const hook = renderHook(() => useAbortSession(value.id, value), { wrapper })
  await act(async () => { await hook.result.current.mutateAsync() })
  expect(usePendingStore.getState().questions.get(value.id)?.[0].id).toBe(question.id)
  hook.unmount()
  client.clear()
})
