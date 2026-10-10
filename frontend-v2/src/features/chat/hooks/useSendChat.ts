import { useCallback } from "react"
import { toast } from "@/shared/ui/Toast"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { useSendMessage, type SendMessageVars, type SendRequest } from "../api/messages"
import { optimisticUserMessage } from "../lib/message"
import { pendingSendIdentity } from "../lib/pending-send"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { useStreamStore } from "../stores/stream"
import { ApiError } from "@/shared/api/http"
import { requestDesktopPanel } from "@/shared/events/desktop"
import { usePendingStore } from "../stores/pending"
import { useSendReceiptStore } from "../stores/send-receipts"

export interface SendOpts {
  model?: string
  variant?: string | null
  videoModel?: string
  videoResolution?: string
  agent?: string
  attachments?: string[]
}

function isFollowupReceipt(response: unknown): boolean {
  return !!response && typeof response === "object" && "delivery" in response && response.delivery === "followup"
}

function isDefiniteRefusal(error: unknown): boolean {
  return error instanceof ApiError && error.status < 500 && error.status !== 408 && error.status !== 429
}

/** Send a prompt in an existing session: optimistic echo + busy status + POST. */
export function useSendChat(sessionId: string, submit?: SendRequest): (text: string, opts?: SendOpts) => Promise<void> {
  const send = useSendMessage(sessionId, submit)
  const errorMessage = useApiErrorMessage()
  const { mutateAsync } = send

  return useCallback(
    async (text, opts) => {
      const trimmed = text.trim()
      if (!trimmed) return
      const body = { text: trimmed, ...opts }
      const identity = await pendingSendIdentity(JSON.stringify([useAuthStore.getState().user?.id,
        useWorkspaceStore.getState().currentId, sessionId]), body)
      const clientMessageId = identity.id
      const receipt = (state: "sending" | "accepted" | "uncertain" | null) =>
        useSendReceiptStore.getState().set(sessionId, clientMessageId, state)
      const store = useStreamStore.getState()
      const previousStatus = store.status.get(sessionId) ?? "idle"
      const oldQuestions = usePendingStore.getState().questions.get(sessionId) ?? []
      store.addMessage(sessionId, optimisticUserMessage(sessionId, trimmed, clientMessageId))
      store.setStatus(sessionId, "busy")
      const optimisticRevision = useStreamStore.getState().statusRevision.get(sessionId)
      store.clearRunError(sessionId)
      receipt("sending")
      const vars: SendMessageVars = {
        text: trimmed,
        model: opts?.model,
        variant: opts?.variant,
        videoModel: opts?.videoModel,
        videoResolution: opts?.videoResolution,
        agent: opts?.agent,
        attachments: opts?.attachments,
        clientMessageId,
      }
      try {
        // mutateAsync rather than mutate: the composer restores the draft on a
        // rejection, and it can only do that if the failure reaches it.
        const response = await mutateAsync(vars)
        identity.confirmed()
        receipt("accepted")
        // The committed new message supersedes only the questions visible
        // before this send, never a newer ask that raced the HTTP response.
        // A queued input does not answer or supersede the current question.
        const queued = isFollowupReceipt(response)
        if (!queued) for (const question of oldQuestions) usePendingStore.getState().removeQuestion(question.id)
      } catch (err) {
        const failed = useStreamStore.getState()
        if (failed.messages.get(sessionId)?.some((message) => message.client_message_id === clientMessageId &&
          !message.id.startsWith("tmp-"))) {
          // The durable WS/history echo can win the race against a lost HTTP
          // response. Do not restore a draft that the server already accepted.
          identity.confirmed()
          receipt("accepted")
          return
        }
        // A delayed HTTP failure cannot undo a newer send or server event.
        if (failed.statusRevision.get(sessionId) === optimisticRevision) failed.setStatus(sessionId, previousStatus)
        // Only a definite refusal removes the echo; a transport error may
        // arrive after the server durably accepted this exact input.
        const refused = isDefiniteRefusal(err)
        if (refused) failed.dropOptimistic(sessionId, clientMessageId)
        receipt(refused ? null : "uncertain")
        if (err instanceof ApiError && err.code === "DESKTOP_NOT_READY") {
          requestDesktopPanel()
        }
        // Definite validation refusals permit an edited request. Unknown
        // outcomes keep their original key until a durable receipt arrives.
        if (refused) identity.confirmed()
        toast("error", errorMessage(err))
        throw err
      }
    },
    [sessionId, mutateAsync, errorMessage],
  )
}
