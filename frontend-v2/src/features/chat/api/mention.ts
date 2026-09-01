// Data hooks for the composer mention menu. Components never fetch directly —
// they consume these (ENGINEERING_SPEC §7). Every key carries the user id so an
// account switch can't serve another user's cache (§7.2).
import { useQuery } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"

export interface FileSearchResult {
  files: string[]
  total?: number
}

export interface MentionSkill {
  name?: string
  description?: string
}

export interface MentionCommand {
  name?: string
  description?: string
}

export function fileSearchPath(
  containerId: string,
  query: string,
  sessionId?: string,
  projectId?: string,
): string {
  const params = new URLSearchParams({ q: query, limit: "20" })
  if (sessionId) params.set("session_id", sessionId)
  if (projectId) params.set("project_id", projectId)
  return `/api/containers/${encodeURIComponent(containerId)}/files/search?${params.toString()}`
}

function useUserId(): string {
  return useAuthStore((s) => s.user?.id ?? "anonymous")
}

/**
 * Sandbox file search for "@" mentions. Caller gates `enabled` (no sandbox /
 * empty query → don't fire); results go stale fast so fresh keystrokes refetch.
 */
interface FileSearchOptions {
  containerId: string | null
  query: string
  enabled: boolean
  sessionId?: string
  projectId?: string
}

export function useFileSearch({ containerId, query, enabled, sessionId, projectId }: FileSearchOptions) {
  const userId = useUserId()
  return useQuery({
    queryKey: ["mention-files", userId, containerId, sessionId, projectId, query] as const,
    queryFn: () =>
      http.get<FileSearchResult>(fileSearchPath(containerId as string, query, sessionId, projectId)),
    enabled: enabled && containerId !== null,
    staleTime: 5_000,
  })
}

export function useSkills() {
  const userId = useUserId()
  return useQuery({
    queryKey: ["mention-skills", userId] as const,
    queryFn: () => http.get<MentionSkill[]>("/api/agent/skill"),
    staleTime: 60_000,
  })
}

// Currently the backend returns []; the menu handles the empty list gracefully.
export function useCommands() {
  const userId = useUserId()
  return useQuery({
    queryKey: ["mention-commands", userId] as const,
    queryFn: () => http.get<MentionCommand[]>("/api/agent/command"),
    staleTime: 60_000,
  })
}
