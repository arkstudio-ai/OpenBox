import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { ApiError, http } from "@/shared/api/http"
import { toast } from "@/shared/ui/Toast"
import type { QuestionDraftAnswer, QuestionRequest } from "@/shared/types/api"
import { usePendingStore } from "../stores/pending"
import { chatKeys } from "./keys"
import { useUserId } from "./messages"

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
    mutationFn: ({ requestId, answers }: { requestId: string; answers: string[][] }) =>
      http.post<{ ok: boolean; session_id: string }>(`/api/agent/question/${requestId}`, { answers }),
    onSuccess: (_data, { requestId }) => {
      usePendingStore.getState().removeQuestion(requestId)
      clearQuestionDraft(userId, requestId)
      void qc.invalidateQueries({ queryKey: chatKeys.questions(userId) })
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
    mutationFn: (requestId: string) => http.post<{ ok: boolean; session_id: string }>(`/api/agent/question/${requestId}/reject`),
    onSuccess: (_data, requestId) => {
      usePendingStore.getState().removeQuestion(requestId)
      clearQuestionDraft(userId, requestId)
      void qc.invalidateQueries({ queryKey: chatKeys.questions(userId) })
      if (_data.session_id) {
        void qc.invalidateQueries({ queryKey: ["session", userId, _data.session_id] })
        void qc.invalidateQueries({ queryKey: chatKeys.messages(userId, _data.session_id) })
      }
    },
    onError: (error, requestId) => failed(error, requestId),
  })
}

function useQuestionFailure() {
  const { t } = useTranslation("chat")
  const userId = useUserId()
  const qc = useQueryClient()
  return (error: Error, requestId: string) => {
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
  try { localStorage.removeItem(questionDraftKey(userId, requestId)) } catch { /* private browsing */ }
}

export function saveQuestionDraft(requestId: string, draft: QuestionDraftAnswer[], revision: number) {
  return http.put<QuestionRequest>(`/api/agent/question/${requestId}/draft`, { draft, revision })
}

export function getQuestion(requestId: string) {
  return http.get<QuestionRequest>(`/api/agent/question/${requestId}`)
}
