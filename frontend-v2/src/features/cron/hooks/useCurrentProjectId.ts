// Resolve the project a conversation belongs to, off the shared workspace
// "sessions" cache (same query key → same cache, ENGINEERING_SPEC §7.2).
import { useQuery } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"

interface SessionRow {
  id: string
  project_id?: string | null
}

export function useCurrentProjectId(sessionId: string | null): string | null {
  const userId = useAuthStore((s) => s.user?.id ?? "anonymous")
  const sessions = useQuery({
    queryKey: ["sessions", userId],
    queryFn: () => http.get<SessionRow[]>("/api/agent/session"),
    staleTime: 30_000,
  })
  if (!sessionId) return null
  return (sessions.data ?? []).find((s) => s.id === sessionId)?.project_id ?? null
}
