// What a chat view shows of its conversation: the newest turns, a catch-up
// once a second while a run is live, and older turns when the reader scrolls up.
//
// Every page lands in the stream store, merged so older pages the reader has
// loaded stay put and live WebSocket deltas are never clobbered. The view used
// to re-read the whole conversation on every open and every second of a run —
// a megabyte a second for a 350-message chat (2026-09-14).
import { useCallback, useEffect } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { toast } from "@/shared/ui/Toast"
import { chatKeys } from "../api/keys"
import { isHistoryCursorGone, loadOlderHistory, useLiveHistory, useMessagesQuery, useUserId } from "../api/messages"
import { useStreamStore } from "../stores/stream"

export function useChatHistory(sessionId: string, live: boolean) {
  const messagesQ = useMessagesQuery(sessionId)
  const liveQ = useLiveHistory(sessionId, live)
  const qc = useQueryClient()
  const userId = useUserId()
  const errorMessage = useApiErrorMessage()

  // Only a newest-turns page can say whether older turns exist; a catch-up
  // page starts at a message already held.
  useEffect(() => {
    if (messagesQ.data) {
      useStreamStore.getState().mergeHistory(sessionId, messagesQ.data.messages, messagesQ.data.has_more)
    }
  }, [messagesQ.data, sessionId])
  useEffect(() => {
    if (!liveQ.data) return
    const { messages, has_more: hasMore, windowed } = liveQ.data
    useStreamStore.getState().mergeHistory(sessionId, messages, windowed ? hasMore : undefined)
  }, [liveQ.data, sessionId])

  // A page's anchor was deleted underneath it (a regenerate or dismiss in
  // another tab). What is held can no longer be placed, so start again from
  // the newest turns.
  const reset = useCallback(() => {
    useStreamStore.getState().clearMessages(sessionId)
    void qc.invalidateQueries({ queryKey: chatKeys.messages(userId, sessionId) })
  }, [qc, userId, sessionId])
  useEffect(() => {
    if (isHistoryCursorGone(liveQ.error)) reset()
  }, [liveQ.error, reset])

  const loadOlder = useCallback(() => {
    loadOlderHistory(sessionId).catch((error: unknown) => {
      if (isHistoryCursorGone(error)) reset()
      else toast("error", errorMessage(error))
    })
  }, [sessionId, reset, errorMessage])

  const paging = useStreamStore((s) => s.history.get(sessionId))
  return {
    messagesQ,
    hasMore: paging?.hasMore ?? false,
    loadingOlder: paging?.loadingOlder ?? false,
    loadOlder,
  }
}
