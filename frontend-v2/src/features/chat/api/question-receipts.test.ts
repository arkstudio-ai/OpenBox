import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { ApiError, http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { postQuestionReply } from "./question"

const binding = { kind: "question" as const, task_id: "task", assistant_session_id: "main",
  workspace_id: "workspace", project_id: "project", run_id: "run", generation: 4,
  request_revision: "a".repeat(64), options_hash: "b".repeat(64) }
beforeEach(() => {
  useAuthStore.setState({ user: { id: "owner" } as never })
  useWorkspaceStore.setState({ currentId: "workspace" })
  vi.spyOn(http, "post").mockResolvedValue({ ok: true, session_id: "execution", state: "accepted" })
})
afterEach(() => vi.restoreAllMocks())

it("keeps one reply identity after an uncertain response, bound to the exact displayed card", async () => {
  vi.mocked(http.post).mockRejectedValueOnce(new TypeError("lost response"))
  await expect(postQuestionReply("owner", "q-retry", { answers: [["Blue"]] }, { binding })).rejects.toThrow("lost response")
  await postQuestionReply("owner", "q-retry", { answers: [["Blue"]] }, { binding })
  const first = vi.mocked(http.post).mock.calls[0][1] as Record<string, unknown>
  expect(vi.mocked(http.post).mock.calls[1][1]).toEqual(first)
  expect(first).toMatchObject({ expected_request_revision: binding.request_revision,
    options_hash: binding.options_hash, source_ref: { kind: "card" }, answers: [["Blue"]] })
  expect(first.reply_id).toEqual(expect.any(String))
  expect(vi.mocked(http.post).mock.calls[1][2]).toEqual({ headers: { "X-Workspace-Id": "workspace" } })
})

it("does not retry or turn a conflict into a different decision", async () => {
  vi.mocked(http.post).mockRejectedValueOnce(new ApiError(409, "QUESTION_CONFLICT", "already answered"))
  await expect(postQuestionReply("owner", "q-conflict", { answers: [["Blue"]] }, { binding })).rejects.toMatchObject({ status: 409 })
  expect(http.post).toHaveBeenCalledTimes(1)
})

it("includes the same immutable binding for skipping a linked question", async () => {
  await postQuestionReply("owner", "q-skip", {}, { binding, reject: true })
  expect(http.post).toHaveBeenCalledWith("/api/agent/question/q-skip/reject", expect.objectContaining({
    expected_request_revision: binding.request_revision, options_hash: binding.options_hash,
    source_ref: { kind: "card" }, reply_id: expect.any(String),
  }), expect.any(Object))
})

it("refuses an old card after the active account or workspace changes", async () => {
  useWorkspaceStore.setState({ currentId: "another-workspace" })
  await expect(postQuestionReply("owner", "q-scope", { answers: [["Blue"]] }, { binding })).rejects.toThrow("workspace changed")
  useWorkspaceStore.setState({ currentId: "workspace" })
  useAuthStore.setState({ user: { id: "other" } as never })
  await expect(postQuestionReply("owner", "q-scope", { answers: [["Blue"]] }, { binding })).rejects.toThrow("account")
  expect(http.post).not.toHaveBeenCalled()
})
