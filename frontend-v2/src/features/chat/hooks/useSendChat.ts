import { useCallback } from "react"
import { toast } from "@/shared/ui/Toast"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { useSendMessage, type SendMessageVars } from "../api/messages"
import { makeClientId, optimisticUserMessage } from "../lib/message"
import { useStreamStore } from "../stores/stream"

export interface SendOpts {
  model?: string
  agent?: string
  attachments?: string[]
}

/** Send a prompt in an existing session: optimistic echo + busy status + POST. */
export function useSendChat(sessionId: string): (text: string, opts?: SendOpts) => Promise<void> {
  const send = useSendMessage(sessionId)
  const errorMessage = useApiErrorMessage()
  const { mutateAsync } = send

  return useCallback(
    async (text, opts) => {
      const trimmed = text.trim()
      if (!trimmed) return
      const clientMessageId = makeClientId()
      const store = useStreamStore.getState()
      store.addMessage(sessionId, optimisticUserMessage(sessionId, trimmed, clientMessageId))
      store.setStatus(sessionId, "busy")
      store.clearRunError(sessionId)
      const vars: SendMessageVars = {
        text: trimmed,
        model: opts?.model,
        agent: opts?.agent,
        attachments: opts?.attachments,
        clientMessageId,
      }
      try {
        // mutateAsync rather than mutate: the composer restores the draft on a
        // rejection, and it can only do that if the failure reaches it.
        await mutateAsync(vars)
      } catch (err) {
        const failed = useStreamStore.getState()
        failed.setStatus(sessionId, "idle")
        // Take the optimistic echo back down. Leaving it there showed the
        // message sitting in the transcript as though it had been sent, which
        // is the opposite of what happened.
        failed.dropOptimistic(sessionId, clientMessageId)
        toast("error", errorMessage(err))
        throw err
      }
    },
    [sessionId, mutateAsync, errorMessage],
  )
}
