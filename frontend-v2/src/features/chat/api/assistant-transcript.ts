import { useEffect, useState } from "react"
import { useQueries, type QueryClient } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import type { MessageWithParts } from "@/shared/types/api"
import { assistantKeys, scopedOptions } from "./assistant"
import { useStreamStore } from "../stores/stream"
import { createHistoryProofReader } from "./history-source-proof"

export interface TranscriptPage { messages: MessageWithParts[] }
const EMPTY: MessageWithParts[] = []
export function reconcileTranscript(previous: TranscriptPage | undefined, next: TranscriptPage): TranscriptPage {
  const held = new Map(previous?.messages.map((message) => [message.id, message]) ?? [])
  return { messages: next.messages.map((message) => {
    const current = held.get(message.id)
    return current && (current.source_checked_at ?? "") > (message.source_checked_at ?? "") ? current : message
  }) }
}

export async function readAssistantMessages(sessionId: string, ids: string[], workspaceId: string | null, signal?: AbortSignal) {
  const params = new URLSearchParams({ session_id: sessionId })
  ids.forEach((id) => params.append("message_ids", id))
  const page = await http.get<TranscriptPage>(`/api/assistant/messages?${params}`, scopedOptions(workspaceId, signal))
  if (page.messages.length !== new Set(ids).size || new Set(page.messages.map((m) => m.id)).size !== page.messages.length ||
    page.messages.some((m) => !ids.includes(m.id) || m.session_id !== sessionId || !m.source_checked_at ||
      !["available", "pending", "unavailable"].includes(m.source_status ?? ""))) throw new Error("Missing current-source projection")
  return page
}

/** Recheck every loaded page; the current-source projection belongs to Query. */
export function useAssistantTranscript(sessionId: string) {
  const userId = useAuthStore((state) => state.user?.id ?? "anonymous")
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  const [foreground, setForeground] = useState(() => ({ visible: document.visibilityState === "visible", epoch: Date.now() }))
  const [readHistoryProof] = useState(createHistoryProofReader)
  useEffect(() => {
    const changed = () => setForeground({ visible: document.visibilityState === "visible", epoch: Date.now() })
    document.addEventListener("visibilitychange", changed)
    return () => document.removeEventListener("visibilitychange", changed)
  }, [])
  const messages = useStreamStore((state) => state.messages.get(sessionId) ?? EMPTY)
  const ids = messages.filter((message) => !message.id.startsWith("tmp-")).map((message) => message.id)
  const chunks = []
  for (let offset = 0; offset < ids.length; offset += 100) chunks.push(ids.slice(offset, offset + 100))
  return useQueries({ queries: chunks.map((selected) => ({
    queryKey: assistantKeys.transcript(userId, workspaceId, sessionId, selected),
    queryFn: ({ signal }: { signal: AbortSignal }) => {
      const held = new Map((useStreamStore.getState().messages.get(sessionId) ?? []).map((message) => [message.id, message]))
      const history = readHistoryProof(selected.map((id) => held.get(id)), { userId, workspaceId }, foreground.epoch)
      // The history endpoint already performed the identical current-source
      // check. Reuse that new response once, then keep normal polls/rechecks.
      if (history) return Promise.resolve({ messages: history })
      return readAssistantMessages(sessionId, selected, workspaceId, signal)
    },
    structuralSharing: (previous: unknown, next: unknown) => reconcileTranscript(previous as TranscriptPage | undefined, next as TranscriptPage),
    enabled: !!workspaceId && userId !== "anonymous" && foreground.visible,
    staleTime: 0, refetchOnMount: "always" as const, refetchInterval: 15_000, retry: false,
  })), combine: (results) => ({
    messages: results.flatMap((result) => result.data?.messages ?? []),
    failed: results.some((result) => !!result.error),
    scopePending: !workspaceId || userId === "anonymous" || !foreground.visible,
    pendingIds: new Set(results.flatMap((result, index) => !result.error &&
      (!result.isFetchedAfterMount || result.dataUpdatedAt < foreground.epoch) ? chunks[index] : [])),
    unavailableIds: new Set(results.flatMap((result, index) => result.error ? chunks[index] : [])),
    pending: !workspaceId || userId === "anonymous" || !foreground.visible || results.some((result) =>
      !result.isFetchedAfterMount || result.dataUpdatedAt < foreground.epoch),
  }) })
}

/** A click-time read also refreshes any mounted transcript pages. */
export function refreshTranscriptPages(qc: QueryClient, key: readonly unknown[], page: TranscriptPage) {
  const updates = new Map(page.messages.map((message) => [message.id, message]))
  qc.setQueriesData<TranscriptPage>({ queryKey: key }, (cached) => cached && ({ messages: cached.messages.map((held) => {
    const next = updates.get(held.id)
    return next && (next.source_checked_at ?? "") >= (held.source_checked_at ?? "") ? next : held
  }) }))
}
