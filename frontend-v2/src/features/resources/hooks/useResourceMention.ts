// Feeds the composer's "@" menu. The chat feature owns how the menu looks and
// how keys move through it; this owns what is in it. The two meet in the
// route, which is the sanctioned way for features to cooperate (§4.2).
//
// The menu opens on the conversation's own project — that is where the file
// someone is about to reference almost always lives — and the switcher lets
// them step out to another project or to everything.
import { useCallback, useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { fetchResources, useResourceProjects, useResourcesQuery } from "@/features/resources/api/resources"
import { ALL_PROJECTS } from "@/features/resources/constants"
import type { Resource, ResourceProject, SourceFilter } from "@/features/resources/types"

export interface ResourceMention {
  projects: ResourceProject[]
  /** Project the menu is scoped to: a project id or "all". */
  project: string
  setProject: (id: string) => void
  source: SourceFilter
  setSource: (source: SourceFilter) => void
  /** Recent items for browsing; searches also cover older resources. */
  items: Resource[]
  loading: boolean
  search: (query: string, signal?: AbortSignal) => Promise<Resource[]>
}

interface SessionRow {
  id: string
  project_id?: string | null
}

/** The conversation's project, read off the shared "sessions" cache (§7.2). */
function useSessionProject(sessionId: string | null): string | null {
  const userId = useAuthStore((s) => s.user?.id ?? "anonymous")
  const sessions = useQuery({
    queryKey: ["sessions", userId],
    queryFn: () => http.get<SessionRow[]>("/api/agent/session"),
    staleTime: 30_000,
  })
  if (!sessionId) return null
  return (sessions.data ?? []).find((s) => s.id === sessionId)?.project_id ?? null
}

/**
 * @param sessionId conversation in progress, or null on the empty chat
 * @param fallbackProject project the empty chat will file its first message in
 */
export function useResourceMention(
  sessionId: string | null,
  fallbackProject?: string | null,
): ResourceMention {
  const sessionProject = useSessionProject(sessionId)
  const home = sessionProject ?? fallbackProject ?? ALL_PROJECTS
  // Overrides the conversation's project only after someone picks another one,
  // and resets when the conversation changes.
  const [picked, setPicked] = useState<string | null>(null)
  const [seenHome, setSeenHome] = useState(home)
  if (seenHome !== home) {
    setSeenHome(home)
    setPicked(null)
  }
  const project = picked ?? home
  const [source, setSource] = useState<SourceFilter>("all")
  const workspaceId = useWorkspaceStore((state) => state.currentId)

  const projects = useResourceProjects()
  const list = useResourcesQuery({ project, source, kind: "all", q: "", sort: "created", limit: 200 }, true)
  const search = useCallback(async (q: string, signal?: AbortSignal) => {
    const page = await fetchResources({ project, source, kind: "all", q, sort: "created", limit: 200 }, workspaceId, signal)
    return page.items
  }, [project, source, workspaceId])

  return useMemo(
    () => ({
      projects: projects.data ?? [],
      project,
      setProject: setPicked,
      source,
      setSource,
      items: list.data?.items ?? [],
      loading: list.isLoading,
      search,
    }),
    [projects.data, project, source, list.data, list.isLoading, search],
  )
}
