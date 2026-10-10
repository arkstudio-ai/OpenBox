// What each reply in a chat drew on: the memories recall found relevant to the user's message
// (backend memory/recalls.py), as they read now: one forgotten since is no longer listed.
import { useQuery } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { useUserId } from "./messages"

export interface RecalledMemory {
  id: string
  summary: string
  type: string | null
  project_id: string | null
}

export function recalledKey(userId: string, sessionId: string) {
  return ["memory-recalled", userId, sessionId] as const
}

/** {user message id: memories} for the chat; one read serves every reply in it. */
export function useRecalledMemories(sessionId: string) {
  const userId = useUserId()
  return useQuery({
    queryKey: recalledKey(userId, sessionId),
    queryFn: () =>
      http.get<{ recalls: Record<string, RecalledMemory[]> }>(
        `/api/memories/recalled/${encodeURIComponent(sessionId)}`,
      ),
    enabled: sessionId.length > 0,
    staleTime: 60_000,
  })
}
