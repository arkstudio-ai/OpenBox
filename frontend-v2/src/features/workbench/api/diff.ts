// Session diff query — `GET /api/agent/session/{id}/diff` → DiffEntry[].
// Invalidated by `usePanelEvents` when the backend publishes `session.diff`.
import { useQuery } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import type { DiffEntry } from "@/shared/types/api"
import { useUserId, workbenchKeys } from "./keys"

export function useDiffQuery(sessionId: string | null, teamRunId?: string | null) {
  const userId = useUserId()
  return useQuery({
    queryKey: teamRunId
      ? [...workbenchKeys.diff(userId, sessionId ?? "none"), "team", teamRunId]
      : workbenchKeys.diff(userId, sessionId ?? "none"),
    queryFn: () =>
      http.get<DiffEntry[]>(
        teamRunId
          ? `/api/team-runs/${encodeURIComponent(teamRunId)}/diff?full=true`
          : `/api/agent/session/${sessionId}/diff?full=true`,
      ),
    enabled: !!sessionId,
  })
}
