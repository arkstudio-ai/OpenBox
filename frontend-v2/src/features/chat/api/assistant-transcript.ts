import { useQueries, type QueryClient, type UseQueryResult } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import type { MessageWithParts } from "@/shared/types/api"
import { assistantKeys, scopedOptions } from "./assistant"
import { useStreamStore } from "../stores/stream"

export interface TranscriptPage { messages: MessageWithParts[] }
const EMPTY: MessageWithParts[] = []
function combine(results: UseQueryResult<TranscriptPage>[]) {
  return { messages: results.flatMap((result) => result.data?.messages ?? []),
    failed: results.some((result) => !!result.error) }
}

export function reconcileTranscript(previous: TranscriptPage | undefined, next: TranscriptPage): TranscriptPage {
  const held = new Map(previous?.messages.map((message) => [message.id, message]) ?? [])
  return { messages: next.messages.map((message) => {
    const current = held.get(message.id)
    return current && (current.source_checked_at ?? "") > (message.source_checked_at ?? "") ? current : message
  }) }
}

export function readAssistantMessages(sessionId: string, ids: string[], workspaceId: string | null, signal?: AbortSignal) {
  const params = new URLSearchParams({ session_id: sessionId })
  ids.forEach((id) => params.append("message_ids", id))
  return http.get<TranscriptPage>(`/api/assistant/messages?${params}`, scopedOptions(workspaceId, signal))
}

/** Recheck every loaded page; the current-source projection belongs to Query. */
export function useAssistantTranscript(sessionId: string) {
  const userId = useAuthStore((state) => state.user?.id ?? "anonymous")
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  const messages = useStreamStore((state) => state.messages.get(sessionId) ?? EMPTY)
  const ids = messages.filter((message) => !message.id.startsWith("tmp-")).map((message) => message.id)
  const chunks = []
  for (let offset = 0; offset < ids.length; offset += 100) chunks.push(ids.slice(offset, offset + 100))
  return useQueries({ queries: chunks.map((selected) => ({
    queryKey: assistantKeys.transcript(userId, workspaceId, sessionId, selected),
    queryFn: ({ signal }: { signal: AbortSignal }) => readAssistantMessages(sessionId, selected, workspaceId, signal),
    structuralSharing: (previous: unknown, next: unknown) => reconcileTranscript(previous as TranscriptPage | undefined, next as TranscriptPage),
    enabled: !!workspaceId && userId !== "anonymous",
    staleTime: 0, refetchOnMount: "always" as const, refetchInterval: 15_000, retry: false,
  })), combine })
}

/** A click-time read also refreshes any mounted transcript pages. */
export function refreshTranscriptPages(qc: QueryClient, key: readonly unknown[], page: TranscriptPage) {
  const updates = new Map(page.messages.map((message) => [message.id, message]))
  qc.setQueriesData<TranscriptPage>({ queryKey: key }, (cached) => cached && ({ messages: cached.messages.map((held) => {
    const next = updates.get(held.id)
    return next && (next.source_checked_at ?? "") >= (held.source_checked_at ?? "") ? next : held
  }) }))
}
