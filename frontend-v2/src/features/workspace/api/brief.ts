// The per-project brief the user and their personal assistant keep: standing
// project context injected into every one of the user's sessions in it.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { workspaceKeys } from "./keys"
import { useUserId } from "./projects"

/** The server's limit, counted in characters (code points). */
export const MAX_BRIEF_CHARS = 6000

export interface ProjectBrief {
  id: string | null
  project_id: string
  content: string
  /** 0 until the first save. */
  revision: number
  updated_by: "user" | "assistant" | null
  created_at?: string | null
  updated_at: string | null
}

const briefPath = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/brief`

export function useProjectBrief(projectId: string) {
  const userId = useUserId()
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  return useQuery({
    queryKey: workspaceKeys.brief(userId, workspaceId, projectId),
    // An older server answers null for "none yet"; the current one an empty revision-0 view.
    queryFn: async ({ signal }) => (await http.get<ProjectBrief | null>(briefPath(projectId), { signal })) ?? null,
    enabled: projectId.length > 0,
    staleTime: 0,
    retry: false,
  })
}

/** `revision` is the one the edit was based on: 0 creates the brief. */
export function useSaveProjectBrief(projectId: string) {
  const userId = useUserId()
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ content, revision }: { content: string; revision: number }) =>
      http.put<ProjectBrief>(briefPath(projectId), { content, expected_revision: revision }),
    onSuccess: (brief) => qc.setQueryData(workspaceKeys.brief(userId, workspaceId, projectId), brief),
  })
}
