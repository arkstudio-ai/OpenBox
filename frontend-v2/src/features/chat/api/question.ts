import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { ApiError, http } from "@/shared/api/http"
import { toast } from "@/shared/ui/Toast"
import type { QuestionDraftAnswer, QuestionRequest } from "@/shared/types/api"
import { usePendingStore } from "../stores/pending"
import { chatKeys } from "./keys"
import { useUserId } from "./messages"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { pendingSendIdentity } from "../lib/pending-send"

export interface QuestionReceipt {
  ok: boolean
  session_id: string
  command_id?: string
  request_id?: string
  task_id?: string
  state?: "accepted" | "applying" | "applied" | "failed"
  accepted_at?: string
  request_kind?: "question" | "permission"
  action?: "once" | "always" | "reject"
  error_code?: string
}

type ReplyVars = { requestId: string; answers: string[][]; attachments?: string[][]; assistant?: QuestionRequest["assistant"] }
type RejectVars = string | { requestId: string; assistant?: QuestionRequest["assistant"] }
const requestIdentity = (value: RejectVars) => typeof value === "string" ? value : value.requestId

export async function postQuestionReply(userId: string, requestId: string, body: Record<string, unknown>,
  { binding, reject = false }: { binding?: QuestionRequest["assistant"]; reject?: boolean } = {}): Promise<QuestionReceipt> {
  const endpoint = `/api/agent/question/${requestId}${reject ? "/reject" : ""}`
  if (!binding) return reject ? http.post<QuestionReceipt>(endpoint) : http.post<QuestionReceipt>(endpoint, body)
  const current = () => useAuthStore.getState().user?.id === userId
    && useWorkspaceStore.getState().currentId === binding.workspace_id
  if (!current()) throw new Error("Question account or workspace changed")
  const payload = { ...body, expected_request_revision: binding.request_revision,
    options_hash: binding.options_hash, source_ref: { kind: "card" } }
  const identity = await pendingSendIdentity(JSON.stringify([userId, binding.workspace_id,
    binding.assistant_session_id, "question-reply"]), { text: JSON.stringify([requestId, reject, payload]) })
  if (!current()) throw new Error("Question account or workspace changed")
  try {
    const receipt = await http.post<QuestionReceipt>(endpoint, { ...payload, reply_id: identity.id },
      { headers: { "X-Workspace-Id": binding.workspace_id } })
    identity.confirmed()
    return receipt
  } catch (error) {
    if (error instanceof ApiError && error.status < 500) identity.confirmed()
    throw error
  }
}

export function useQuestionsQuery() {
  const userId = useUserId()
  return useQuery({
    queryKey: chatKeys.questions(userId),
    queryFn: async ({ signal }) => {
      const read = usePendingStore.getState().beginQuestionRead()
      const items = await http.get<QuestionRequest[]>("/api/agent/question", { signal })
      return { items, read }
    },
    refetchOnMount: "always",
  })
}

export function useReplyQuestion() {
  const failed = useQuestionFailure()
  const userId = useUserId()
  const qc = useQueryClient()
  return useMutation({
    // One array of chosen labels per question, in the order they were asked —
    // the shape the server has always expected. It used to be sent flat, so a
    // reply was rejected before it reached the agent.
    mutationFn: ({ requestId, answers, attachments, assistant }: ReplyVars) =>
      postQuestionReply(userId, requestId, {
        answers, ...(attachments?.some((ids) => ids.length > 0) ? { attachments } : {}),
      }, { binding: assistant }),
    onSuccess: (_data, { requestId, assistant }) => {
      if (assistant && (useAuthStore.getState().user?.id !== userId
        || useWorkspaceStore.getState().currentId !== assistant.workspace_id)) return
      usePendingStore.getState().removeQuestion(requestId)
      clearQuestionDraft(userId, requestId)
      void qc.invalidateQueries({ queryKey: chatKeys.questions(userId) })
      void qc.invalidateQueries({ queryKey: ["assistant", userId] })
      if (_data.session_id) {
        void qc.invalidateQueries({ queryKey: ["session", userId, _data.session_id] })
        void qc.invalidateQueries({ queryKey: chatKeys.messages(userId, _data.session_id) })
      }
    },
    onError: (error, { requestId }) => failed(error, requestId),
  })
}

export function useRejectQuestion() {
  const failed = useQuestionFailure()
  const userId = useUserId()
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (value: RejectVars) => postQuestionReply(userId, requestIdentity(value), {},
      { binding: typeof value === "string" ? undefined : value.assistant, reject: true }),
    onSuccess: (_data, value) => {
      if (typeof value !== "string" && value.assistant && (useAuthStore.getState().user?.id !== userId
        || useWorkspaceStore.getState().currentId !== value.assistant.workspace_id)) return
      const requestId = requestIdentity(value)
      usePendingStore.getState().removeQuestion(requestId)
      clearQuestionDraft(userId, requestId)
      void qc.invalidateQueries({ queryKey: chatKeys.questions(userId) })
      void qc.invalidateQueries({ queryKey: ["assistant", userId] })
      if (_data.session_id) {
        void qc.invalidateQueries({ queryKey: ["session", userId, _data.session_id] })
        void qc.invalidateQueries({ queryKey: chatKeys.messages(userId, _data.session_id) })
      }
    },
    onError: (error, value) => failed(error, requestIdentity(value)),
  })
}

function useQuestionFailure() {
  const { t } = useTranslation("chat")
  const userId = useUserId()
  const qc = useQueryClient()
  return (error: Error, requestId: string) => {
    void qc.invalidateQueries({ queryKey: ["assistant", userId] })
    if (error instanceof ApiError && (error.status === 404 || error.status === 410)) {
      usePendingStore.getState().removeQuestion(requestId)
      clearQuestionDraft(userId, requestId)
      toast("error", t("question.gone"))
      void qc.invalidateQueries({ queryKey: chatKeys.questions(userId) })
    } else {
      toast("error", t(error instanceof ApiError && error.status === 409 ? "question.conflict" : "question.submitFailed"))
      if (error instanceof ApiError && error.status === 409) {
        void qc.invalidateQueries({ queryKey: chatKeys.questions(userId) })
      }
    }
  }
}

export function questionDraftKey(userId: string, requestId: string): string {
  return `openbox:question-draft:${userId}:${requestId}`
}

export function clearQuestionDraft(userId: string, requestId: string): void {
  try {
    const key = questionDraftKey(userId, requestId)
    localStorage.removeItem(key)
    localStorage.removeItem(`${key}:page`)
  } catch { /* private browsing */ }
}

export function saveQuestionDraft(requestId: string, draft: QuestionDraftAnswer[], revision: number) {
  return http.put<QuestionRequest>(`/api/agent/question/${requestId}/draft`, { draft, revision })
}

export function getQuestion(requestId: string) {
  return http.get<QuestionRequest>(`/api/agent/question/${requestId}`)
}
