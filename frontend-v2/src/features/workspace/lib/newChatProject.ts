import type { Project } from "@/shared/types/api"

interface Args {
  /** `?project=` from the URL — set when a project row or the top button
   *  opened this page, so it wins. */
  requested: string | null
  /** The sidebar's current project (persisted). */
  selected: string | null
  projects: Project[]
}

interface Resolved {
  projectId?: string
  projectName?: string
}

/** Which project a fresh chat files under. The URL wins; otherwise the
 *  sidebar's current project, as long as it still exists — a selection left
 *  over from a deleted project must not become the new session's home. */
export function resolveNewChatProject({ requested, selected, projects }: Args): Resolved {
  const fallback = selected && projects.some((p) => p.id === selected) ? selected : null
  const projectId = requested ?? fallback
  if (!projectId) return {}
  return { projectId, projectName: projects.find((p) => p.id === projectId)?.name }
}
