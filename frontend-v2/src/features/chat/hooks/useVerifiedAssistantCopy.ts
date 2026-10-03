import { useContext, useState } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { useCopy } from "@/shared/hooks/useCopy"
import { toast } from "@/shared/ui/Toast"
import { assistantKeys } from "../api/assistant"
import { readAssistantMessages, refreshTranscriptPages } from "../api/assistant-transcript"
import { buildAssistantContentView } from "../lib/content-view"
import { AssistantReadContext } from "./assistant-read-context"

export function useVerifiedAssistantCopy(sessionId: string, messageId: string, content: string) {
  const context = useContext(AssistantReadContext)
  const { copied, copy } = useCopy()
  const [checking, setChecking] = useState(false)
  const qc = useQueryClient()
  const { t } = useTranslation("chat")
  const errorMessage = useApiErrorMessage()

  const copyReply = async () => {
    if (!context) { copy(content); return }
    const userId = useAuthStore.getState().user?.id
    const workspaceId = useWorkspaceStore.getState().currentId
    if (checking || !userId || !workspaceId || context.sourcesAvailable === false) return
    setChecking(true)
    try {
      const page = await readAssistantMessages(sessionId, [messageId], workspaceId)
      // An account/workspace change while the read was in flight ends this action.
      if (useAuthStore.getState().user?.id !== userId || useWorkspaceStore.getState().currentId !== workspaceId) return
      refreshTranscriptPages(qc, assistantKeys.transcripts(userId, workspaceId, sessionId), page)
      const message = page.messages.find((item) => item.id === messageId && item.session_id === sessionId)
      if (message?.source_status !== "available") { toast("error", t("assistant.sourceUnavailable")); return }
      const freshText = buildAssistantContentView([message], false).finalText
      if (freshText.trim()) copy(freshText)
    } catch (error) {
      toast("error", errorMessage(error))
    } finally { setChecking(false) }
  }
  return { copied, checking, copyReply }
}
