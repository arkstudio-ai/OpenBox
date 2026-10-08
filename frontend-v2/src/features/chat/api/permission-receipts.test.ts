import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { ApiError, http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { postPermissionReply } from "./permission"

const assistant = { kind: "permission" as const, task_id: "task", assistant_session_id: "main",
  workspace_id: "workspace", project_id: "project", run_id: "run", generation: 3,
  request_revision: "a".repeat(64), options_hash: "b".repeat(64) }
beforeEach(() => {
  useAuthStore.setState({ user: { id: "owner" } as never })
  useWorkspaceStore.setState({ currentId: "workspace" })
  vi.spyOn(http, "post").mockResolvedValue({ ok: true, session_id: "execution", state: "applying" })
})
afterEach(() => vi.restoreAllMocks())

it("reuses the exact reply identity after a lost response without broadening once to always", async () => {
  const body = { requestId: "p-lost", action: "allow" as const, assistant }
  vi.mocked(http.post).mockRejectedValueOnce(new TypeError("response lost"))
  await expect(postPermissionReply("owner", body)).rejects.toThrow("response lost")
  expect((await postPermissionReply("owner", body)).state).toBe("applying")
  const calls = vi.mocked(http.post).mock.calls
  expect(calls[0]).toEqual(calls[1])
  expect(calls[0][1]).toMatchObject({ action: "once", reply_id: expect.any(String),
    expected_request_revision: assistant.request_revision, options_hash: assistant.options_hash,
    source_ref: { kind: "card" } })
})

it.each([409, 410])("does not automatically submit a replacement after HTTP %s", async (status) => {
  vi.mocked(http.post).mockRejectedValueOnce(new ApiError(status, "PERMISSION_GONE", "unavailable"))
  await expect(postPermissionReply("owner", { requestId: "p-conflict", action: "allow", assistant }))
    .rejects.toMatchObject({ status })
  expect(http.post).toHaveBeenCalledTimes(1)
})

it("refuses a stale account/workspace binding before sending", async () => {
  useWorkspaceStore.setState({ currentId: "elsewhere" })
  await expect(postPermissionReply("owner", { requestId: "p-old", action: "allow_always", assistant }))
    .rejects.toThrow("workspace changed")
  expect(http.post).not.toHaveBeenCalled()
})
