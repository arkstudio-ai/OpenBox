import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import type { Session } from "@/shared/types/api"
import { workspaceKeys } from "./keys"
import { useUserId } from "./projects"
import { useWorkspaceStore } from "@/shared/api/workspace-store"

export function useSessionsQuery() {
  const userId = useUserId()
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  return useQuery({
    queryKey: workspaceKeys.sessions(userId, workspaceId),
    // Fetch all sessions once; the sidebar groups by project client-side so
    // switching projects never refetches.
    queryFn: () => http.get<Session[]>("/api/agent/session"),
    staleTime: 30_000,
  })
}

/** A conversation the sidebar search found: by its title, or by what was said in it. */
export interface SessionSearchHit {
  session_id: string
  title: string
  project_id: string | null
  kind: string
  match: "title" | "content"
  /** The words around the newest matching message, on one line; empty for a title-only match. */
  snippet: string
  role: "user" | "assistant" | null
  time: string
}

export function useSessionSearch(query: string) {
  const userId = useUserId()
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  return useQuery({
    queryKey: workspaceKeys.sessionSearch(userId, workspaceId, query),
    queryFn: () =>
      http.get<SessionSearchHit[]>(`/api/agent/session/search?${new URLSearchParams({ q: query })}`),
    enabled: query.length > 0,
    staleTime: 15_000,
    // The last results stay up while the next query runs, so the list does not blink empty.
    placeholderData: keepPreviousData,
  })
}

export function useRenameSession() {
  const userId = useUserId()
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) =>
      http.patch<Session>(`/api/agent/session/${id}`, { title }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: workspaceKeys.sessions(userId, workspaceId) }),
  })
}

export function useDeleteSession() {
  const userId = useUserId()
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => http.delete<{ ok: boolean }>(`/api/agent/session/${id}`),
    onSuccess: () => void qc.invalidateQueries({ queryKey: workspaceKeys.sessions(userId, workspaceId) }),
  })
}
