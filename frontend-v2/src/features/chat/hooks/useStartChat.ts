import { assertRecordingScope, getRecordingScope, type RecordedVoice } from "../api/recorded-voice"
import { useCallback } from "react"
import { toast } from "@/shared/ui/Toast"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { sendPromptAsync, useCreateSession } from "../api/messages"
import { makeClientId, optimisticUserMessage } from "../lib/message"
import { useStreamStore } from "../stores/stream"
import { ApiError } from "@/shared/api/http"
import { requestDesktopPanel } from "@/shared/events/desktop"

export interface StartOpts {
  voiceMessage?: RecordedVoice
  model?: string
  variant?: string | null
  videoModel?: string
  videoResolution?: string
  agent?: string
  projectId?: string
  attachments?: string[]
}

/**
 * From the empty state: create a session, seed the first message, hand the new
 * id to `onSession` (the route navigates), then send the prompt. Navigation is
 * injected so the feature never depends on the app router.
 */
export function useStartChat(
  onSession: (sessionId: string) => void,
): (text: string, opts?: StartOpts) => Promise<void> {
  const create = useCreateSession()
  const errorMessage = useApiErrorMessage()

  return useCallback(
    async (text, opts) => {
      const choices = opts ?? {}
      const scope = getRecordingScope()
      const trimmed = text.trim()
      if (!trimmed) return
      let sessionId: string
      try {
        const session = choices.voiceMessage?.session_id ? { id: choices.voiceMessage.session_id } : await create.mutateAsync({
          projectId: choices.projectId,
          model: choices.model,
          variant: choices.variant,
          agent: choices.agent,
        })
        sessionId = session.id
        if (choices.voiceMessage) {
          choices.voiceMessage.session_id = sessionId
          choices.voiceMessage.saveSession?.(sessionId)
        }
      } catch (err) {
        toast("error", errorMessage(err))
        // Rethrow so the composer knows the send never happened and can put
        // the draft back. Swallowing it here left an empty box that read as
        // "sent".
        throw err
      }

      if (choices.voiceMessage) assertRecordingScope(scope)
      const clientMessageId = choices.voiceMessage?.client_id ?? makeClientId()
      const store = useStreamStore.getState()
      store.addMessage(sessionId, optimisticUserMessage(sessionId, trimmed, clientMessageId, choices.voiceMessage))
      store.setStatus(sessionId, "busy")
      if (!choices.voiceMessage) onSession(sessionId)

      try {
        await sendPromptAsync(sessionId, {
          text: trimmed,
          model: choices.model,
          variant: choices.variant,
          videoModel: choices.videoModel,
          videoResolution: choices.videoResolution,
          agent: choices.agent,
          attachments: choices.attachments,
          voiceMessage: choices.voiceMessage,
          clientMessageId,
        })
        if (choices.voiceMessage) onSession(sessionId)
      } catch (err) {
        useStreamStore.getState().setStatus(sessionId, "idle")
        if (err instanceof ApiError && err.code === "DESKTOP_NOT_READY") {
          requestDesktopPanel()
        }
        toast("error", errorMessage(err))
        throw err
      }
    },
    [create, onSession, errorMessage],
  )
}
