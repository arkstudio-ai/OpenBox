import { useMemo } from "react"
import { useTranslation } from "react-i18next"
import { useLocation, useMatch, useSearchParams } from "react-router"
import { paths, routePatterns } from "@/shared/router/paths"
import type { Session } from "@/shared/types/api"
import { useProjectsQuery } from "../api/projects"
import { useSessionsQuery } from "../api/sessions"
import { useWorkspaceUi } from "../stores/ui"
import { resolveNewChatProject } from "../lib/newChatProject"
import { standalonePage, type StandalonePage } from "../lib/standalonePage"

type Translate = (key: string, options?: Record<string, unknown>) => string

function pageHeading(page: StandalonePage, t: Translate): { title: string; subtitle: string } {
  switch (page) {
    case "billing":
      return { title: t("billing"), subtitle: "" }
    case "settings":
      return { title: t("settings"), subtitle: t("settings:subtitle", { ns: "settings" }) }
    case "cron":
      return { title: t("scheduledTasks"), subtitle: t("scheduledTasksHint") }
    case "resources":
      return { title: t("resourceCenter"), subtitle: t("resourceCenterHint") }
    case "authCenter":
      return { title: t("authCenter"), subtitle: t("authCenterHint") }
    case "skills":
      return { title: t("skillCenter"), subtitle: t("skillCenterHint") }
    case "admin":
      return { title: t("adminConsole"), subtitle: t("adminConsoleHint") }
  }
}

interface Heading {
  /** The standalone page in view, or null on the chat surface. */
  page: StandalonePage | null
  /** The open conversation, when the URL names one that exists. */
  session: Session | null
  title: string
  subtitle: string
}

/** What the topbar calls the current page. Centres name themselves; the
 *  greeting page is "new chat" plus the project the first message will be
 *  filed under; a session shows its own title and project. */
export function useTopbarHeading(): Heading {
  const { t } = useTranslation("workspace")
  // Only a chat URL names the viewer's conversation; admin pages reuse the
  // `:sessionId` segment for sessions that belong to other people.
  const sessionId = useMatch(`${paths.app}/${routePatterns.chat}`)?.params.sessionId
  const location = useLocation()
  const [params] = useSearchParams()
  const sessions = useSessionsQuery()
  const projects = useProjectsQuery()
  const selectedProject = useWorkspaceUi((s) => s.selectedProject)

  const page = standalonePage(location.pathname)
  const session = useMemo(
    () => (sessions.data ?? []).find((s) => s.id === sessionId) ?? null,
    [sessions.data, sessionId],
  )
  const project = useMemo(
    () => (projects.data ?? []).find((p) => p.id === session?.project_id) ?? null,
    [projects.data, session],
  )

  if (page) return { page, session, ...pageHeading(page, t) }
  if (!sessionId) {
    const { projectName } = resolveNewChatProject({
      requested: params.get("project"),
      selected: selectedProject,
      projects: projects.data ?? [],
    })
    return { page, session, title: t("newChat"), subtitle: projectName ?? t("unsorted") }
  }
  return {
    page,
    session,
    title: session?.title ?? t("untitledChat"),
    subtitle: project?.name ?? t("unsorted"),
  }
}
