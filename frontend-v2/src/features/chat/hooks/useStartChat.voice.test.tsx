import { act, cleanup, renderHook } from "@testing-library/react"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { useStartChat } from "./useStartChat"
import type { RecordedVoice } from "../api/recorded-voice"

const api = vi.hoisted(() => ({ create: vi.fn(), send: vi.fn() }))
vi.mock("../api/messages", () => ({ useCreateSession: () => ({ mutateAsync: api.create }),
  sendPromptAsync: (...args: unknown[]) => api.send(...args) }))
vi.mock("@/shared/hooks/useApiErrorMessage", () => ({ useApiErrorMessage: () => () => "failed" }))
vi.mock("@/shared/ui/Toast", () => ({ toast: vi.fn() }))
afterEach(cleanup)
beforeEach(() => {
  vi.resetAllMocks()
  useAuthStore.setState({ user: { id: "owner" } as NonNullable<ReturnType<typeof useAuthStore.getState>["user"]> })
  useWorkspaceStore.setState({ currentId: "workspace" })
})

it("persists a new voice session before dispatch and retries in it after a lost response", async () => {
  api.create.mockResolvedValue({ id: "created" })
  api.send.mockRejectedValueOnce(new Error("offline")).mockResolvedValue({ ok: true })
  const navigate = vi.fn(), saveSession = vi.fn()
  const voice: RecordedVoice = { asset_id: "audio", duration_ms: 2000, client_id: "same-client", saveSession }
  const { result } = renderHook(() => useStartChat(navigate))
  await act(async () => { await expect(result.current("不要加辣", { voiceMessage: voice })).rejects.toThrow("offline") })
  expect(voice.session_id).toBe("created")
  expect(saveSession.mock.invocationCallOrder[0]).toBeLessThan(api.send.mock.invocationCallOrder[0])
  expect(navigate).not.toHaveBeenCalled()
  await act(async () => { await result.current("不要加辣", { voiceMessage: voice }) })
  expect(api.create).toHaveBeenCalledTimes(1)
  expect(api.send.mock.calls.map(([session, body]) => [session, body.clientMessageId])).toEqual([
    ["created", "same-client"], ["created", "same-client"],
  ])
  expect(navigate).toHaveBeenCalledWith("created")
})

it("a workspace switch while creating the session cannot dispatch the old voice", async () => {
  api.create.mockImplementation(async () => {
    useWorkspaceStore.setState({ currentId: "different-workspace" })
    return { id: "original-session" }
  })
  const navigate = vi.fn()
  const { result } = renderHook(() => useStartChat(navigate))
  await act(async () => {
    await expect(result.current("不要加辣", { voiceMessage: {
      asset_id: "audio", duration_ms: 2000, client_id: "scope-test",
    } })).rejects.toThrow("Conversation changed")
  })
  expect(api.send).not.toHaveBeenCalled()
  expect(navigate).not.toHaveBeenCalled()
})
