import { useContext, useEffect, useRef, useState } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { useCopy } from "@/shared/hooks/useCopy"
import { toast } from "@/shared/ui/Toast"
import { assistantKeys } from "../api/assistant"
import { readAssistantMessages, refreshTranscriptPages, type TranscriptPage } from "../api/assistant-transcript"
import { buildAssistantContentView } from "../lib/content-view"
import { sourceDenied } from "../lib/source-projection"
import { AssistantReadContext } from "./assistant-read-context"

function currentScope(userId: string, workspaceId: string) {
  return useAuthStore.getState().user?.id === userId && useWorkspaceStore.getState().currentId === workspaceId
}

function copyText(page: TranscriptPage, content: string, mode: "answer" | "text" | "fragment") {
  const contains = (value: unknown): boolean => typeof value === "string" ? value.includes(content)
    : Array.isArray(value) ? value.some(contains) : !!value && typeof value === "object" && Object.values(value).some(contains)
  if (mode === "fragment") return page.messages.some((m) => contains(m.parts)) ? content : ""
  if (mode === "text") return page.messages.flatMap((m) => m.parts.flatMap((p) =>
    p.type === "text" && !p.synthetic ? [p.text] : [])).join("\n").split("\n\n[attachments]\n")[0].trim()
  return buildAssistantContentView(page.messages, false).finalText
}

export function useVerifiedAssistantCopy(sessionId: string, messageId: string | string[], content: string,
  mode: "answer" | "text" | "fragment" = "answer") {
  const context = useContext(AssistantReadContext)
  const { copied, copy } = useCopy()
  const [checking, setChecking] = useState(false)
  const qc = useQueryClient()
  const { t } = useTranslation("chat")
  const errorMessage = useApiErrorMessage()
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])

  const copyReply = async () => {
    if (!context) { copy(content); return }
    const userId = useAuthStore.getState().user?.id
    const workspaceId = useWorkspaceStore.getState().currentId
    if (checking || !userId || !workspaceId || context.sourcesAvailable === false || context.sourcesPending) return
    setChecking(true)
    try {
      const ids = [...new Set(typeof messageId === "string" ? [messageId] : messageId)].filter((id) => !id.startsWith("tmp-"))
      if (!ids.length) return
      const received = await readAssistantMessages(sessionId, ids, workspaceId)
      const page = { messages: [...received.messages] }
      // An account/workspace change while the read was in flight ends this action.
      if (!mounted.current || !currentScope(userId, workspaceId)) return
      const key = assistantKeys.transcripts(userId, workspaceId, sessionId)
      for (const [, cached] of qc.getQueriesData<TranscriptPage>({ queryKey: key })) {
        for (const held of cached?.messages ?? []) {
          const index = page.messages.findIndex((m) => m.id === held.id && (m.source_checked_at ?? "") < (held.source_checked_at ?? ""))
          if (index >= 0) page.messages[index] = held
        }
      }
      refreshTranscriptPages(qc, key, page)
      if (page.messages.length !== ids.length || page.messages.some((m) => m.session_id !== sessionId || !ids.includes(m.id) || m.source_status !== "available" || sourceDenied(m.id, context, m))) {
        toast("error", t("assistant.sourceUnavailable")); return
      }
      const freshText = copyText(page, content, mode)
      if (freshText.trim()) copy(freshText)
    } catch (error) {
      if (!mounted.current || !currentScope(userId, workspaceId)) return
      void qc.resetQueries({ queryKey: assistantKeys.transcripts(userId, workspaceId, sessionId) })
      toast("error", errorMessage(error))
    } finally { setChecking(false) }
  }
  return { copied, checking, copyReply }
}
