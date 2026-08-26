// The single WS → store bridge. Mounted once on ChatRoute: every message/part/
// tool event lands in the stream store; todo.updated invalidates the todo query;
// permission/question prompts land in the pending store; a reconnect refetches
// the current session snapshot and the pending lists it may have missed.
import { useEffect } from "react"
import { useTranslation } from "react-i18next"
import { toast } from "@/shared/ui/Toast"
import { useQueryClient } from "@tanstack/react-query"
import { wsClient } from "@/shared/ws/client"
import { chatKeys } from "../api/keys"
import { useUserId } from "../api/messages"
import { usePendingStore } from "../stores/pending"
import { useStreamStore } from "../stores/stream"

/** Copy for a run that ended in failure.
 *
 * Prefers a known code, falls back to whatever the server said, and only then
 * to the generic line — the upstream reason ("Configured account requires
 * re-authentication") is the one thing that tells someone what to fix.
 */
function useRunFailureMessage() {
  const { t } = useTranslation("errors")
  return (error?: { message?: string; code?: string }): string => {
    if (error?.code) {
      const byCode = t(error.code, { defaultValue: "" })
      if (byCode) return byCode
    }
    const detail = error?.message?.trim()
    return detail || t("runFailed")
  }
}

export function useChatEvents(sessionId: string): void {
  const qc = useQueryClient()
  const userId = useUserId()
  const runFailureMessage = useRunFailureMessage()

  useEffect(() => {
    // Ensure the socket is up while a chat is open (idempotent; never disconnects
    // here — the connection is app-global).
    void wsClient.connect()

    const stream = useStreamStore.getState()
    const pending = usePendingStore.getState()
    const offs: Array<() => void> = [
      wsClient.on("message.created", (d) => stream.addMessage(d.sessionId, d.message)),
      wsClient.on("message.updated", (d) => stream.updateMessage(d.sessionId, d.message)),
      wsClient.on("message.text_delta", (d) =>
        stream.appendPartDelta(d.sessionId, d.messageId, d.partId, d.text),
      ),
      wsClient.on("part.created", (d) => stream.addPart(d.sessionId, d.messageId, d.part)),
      wsClient.on("part.updated", (d) => stream.updatePart(d.sessionId, d.messageId, d.part)),
      wsClient.on("part.delta", (d) => stream.appendPartDelta(d.sessionId, d.messageId, d.partId, d.delta)),
      wsClient.on("tool.running", (d) => stream.updateToolStatus(d.sessionId, d.partId, "running", d.data)),
      wsClient.on("tool.completed", (d) =>
        stream.updateToolStatus(d.sessionId, d.partId, "completed", d.data),
      ),
      wsClient.on("tool.error", (d) => stream.updateToolStatus(d.sessionId, d.partId, "error", d.data)),
      wsClient.on("session.status", (d) => {
        stream.setStatus(d.sessionId, d.status)
        // A fresh run supersedes whatever the last one failed with.
        if (d.status === "busy") stream.clearRunError(d.sessionId)
        if (d.status === "retry" && d.attempt) {
          stream.setRetry(d.sessionId, d.attempt, d.maxAttempts ?? d.attempt)
        }
        qc.setQueryData(["session", userId, d.sessionId], (old: object | undefined) =>
          old ? { ...old, status: d.status } : old,
        )
        // The terminal idle/error edge is also a consistency barrier: pull
        // the final full parts in case this tab missed the last delta/update.
        if (d.status === "idle" || d.status === "error") {
          void qc.invalidateQueries({ queryKey: chatKeys.messages(userId, d.sessionId) })
        }
      }),
      wsClient.on("session.finalizing", (d) => stream.setStatus(d.sessionId, "finalizing")),
      wsClient.on("session.error", (d) => {
        stream.setStatus(d.sessionId, "error")
        // Say it twice, deliberately. The toast is what someone sees if they
        // are looking; the line above the composer is what remains for someone
        // who was not, or who dismissed the toast — without it, a failed run
        // leaves a screen that looks exactly like a working one.
        const message = runFailureMessage(d.error)
        toast("error", message)
        stream.setRunError(d.sessionId, message)
      }),
      wsClient.on("session.title", () => void qc.invalidateQueries({ queryKey: ["sessions", userId] })),
      // Also the single session, not just the sidebar list: the composer's
      // mode picker reads that record, and the agent changes underneath it
      // whenever the model enters or leaves plan mode. Without this the
      // picker kept claiming the old mode until something else refetched.
      wsClient.on("session.updated", (d) => {
        void qc.invalidateQueries({ queryKey: ["sessions", userId] })
        if (d.sessionId) void qc.invalidateQueries({ queryKey: ["session", userId, d.sessionId] })
      }),
      wsClient.on(
        "todo.updated",
        (d) => void qc.invalidateQueries({ queryKey: chatKeys.todo(userId, d.sessionId) }),
      ),
      wsClient.on("permission.asked", (d) => pending.addPermission(d)),
      wsClient.on("permission.replied", (d) => pending.removePermission(d.request_id)),
      wsClient.on("question.asked", (d) => pending.addQuestion(d)),
      wsClient.on("question.replied", (d) => pending.removeQuestion(d.request_id)),
      wsClient.on("question.rejected", (d) => pending.removeQuestion(d.request_id)),
      wsClient.on("__connected", () => {
        if (sessionId) void qc.invalidateQueries({ queryKey: chatKeys.messages(userId, sessionId) })
        if (sessionId) void qc.invalidateQueries({ queryKey: ["session", userId, sessionId] })
        void qc.invalidateQueries({ queryKey: chatKeys.permissions(userId) })
        void qc.invalidateQueries({ queryKey: chatKeys.questions(userId) })
      }),
    ]
    return () => {
      for (const off of offs) off()
    }
  }, [qc, userId, sessionId, runFailureMessage])
}
