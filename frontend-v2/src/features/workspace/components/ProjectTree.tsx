import { useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { useNavigate, useParams } from "react-router"
import { Clock, MessageSquare } from "lucide-react"
import type { Project, Session } from "@/shared/types/api"
import { Dialog, DialogActions, DialogBody, DialogTitle } from "@/shared/ui/Dialog"
import { paths } from "@/shared/router/paths"
import { cn } from "@/shared/lib/cn"
import { useDeleteProject } from "../api/projects"
import { useDeleteSession } from "../api/sessions"
import { useWorkspaceUi, type SessionFilter } from "../stores/ui"
import { ProjectRow } from "./ProjectRow"
import { SessionRow } from "./SessionRow"

/** Tiny icon segment under a project header: conversations (default) / cron
 *  runs. Rendered only when the project actually has cron run sessions. */
function FilterToggle({
  mode,
  onChange,
  cronCount,
}: {
  mode: SessionFilter
  onChange: (mode: SessionFilter) => void
  cronCount: number
}) {
  const { t } = useTranslation("workspace")
  const segment = (value: SessionFilter, label: string, icon: React.ReactNode) => (
    <button
      type="button"
      title={label}
      aria-label={label}
      aria-pressed={mode === value}
      onClick={() => onChange(value)}
      className={cn(
        "flex h-6 items-center gap-1 rounded-full px-2 text-[11px]",
        mode === value ? "bg-n200 text-ink" : "text-n600 hover:bg-hairsoft",
      )}
    >
      {icon}
      {value === "cron" && cronCount > 0 && <span>{cronCount}</span>}
    </button>
  )
  return (
    <div className="mb-0.5 flex items-center gap-0.5 ps-7">
      {segment("chats", t("filter.chats"), <MessageSquare size={12} strokeWidth={2.2} />)}
      {segment("cron", t("filter.cron"), <Clock size={12} strokeWidth={2.2} />)}
    </div>
  )
}

interface ProjectTreeProps {
  projects: Project[]
  sessions: Session[]
  searching: boolean
}

interface Group {
  project: Project | null
  sessions: Session[]
}

export function ProjectTree({ projects, sessions, searching }: ProjectTreeProps) {
  const { t } = useTranslation("workspace")
  const navigate = useNavigate()
  const { sessionId: activeSessionId } = useParams()
  const deleteProject = useDeleteProject()
  const deleteSession = useDeleteSession()
  const sessionFilter = useWorkspaceUi((s) => s.sessionFilter)
  const setSessionFilter = useWorkspaceUi((s) => s.setSessionFilter)
  const [confirmProject, setConfirmProject] = useState<Project | null>(null)
  const [confirmSession, setConfirmSession] = useState<Session | null>(null)

  const groups = useMemo<Group[]>(() => {
    const byProject = new Map<string, Session[]>()
    const loose: Session[] = []
    for (const s of sessions) {
      if (s.project_id) {
        const list = byProject.get(s.project_id) ?? []
        list.push(s)
        byProject.set(s.project_id, list)
      } else {
        loose.push(s)
      }
    }
    const result: Group[] = projects.map((p) => ({ project: p, sessions: byProject.get(p.id) ?? [] }))
    if (loose.length > 0) result.push({ project: null, sessions: loose })
    return result
  }, [projects, sessions])

  const onDeleteProject = () => {
    if (!confirmProject) return
    deleteProject.mutate(confirmProject.id)
    const ui = useWorkspaceUi.getState()
    if (ui.selectedProject === confirmProject.id) ui.selectProject(null)
    if (groups.some((g) => g.project?.id === confirmProject.id && g.sessions.some((s) => s.id === activeSessionId))) {
      navigate(paths.app)
    }
    setConfirmProject(null)
  }

  const onDeleteSession = () => {
    if (!confirmSession) return
    deleteSession.mutate(confirmSession.id)
    if (confirmSession.id === activeSessionId) navigate(paths.app)
    setConfirmSession(null)
  }

  return (
    <>
      {groups.map((g) => {
        const groupId = g.project?.id ?? "unsorted"
        const crons = g.sessions.filter((s) => s.kind === "cron")
        const mode = sessionFilter[groupId] ?? "chats"
        // While searching, matches from both kinds show — a filter that hides
        // a title you just typed reads as a broken search.
        const visible = searching
          ? g.sessions
          : g.sessions.filter((s) => (mode === "cron" ? s.kind === "cron" : s.kind !== "cron"))
        return (
          <div key={groupId} className="flex flex-col">
            <ProjectRow
              project={g.project}
              forceExpanded={searching}
              onAskDelete={() => g.project && setConfirmProject(g.project)}
            >
              {crons.length > 0 && !searching && (
                <FilterToggle
                  mode={mode}
                  cronCount={crons.length}
                  onChange={(m) => setSessionFilter(groupId, m)}
                />
              )}
              {visible.map((s) => (
                <SessionRow
                  key={s.id}
                  session={s}
                  active={s.id === activeSessionId}
                  onAskDelete={() => setConfirmSession(s)}
                />
              ))}
              {visible.length === 0 && (
                <div className="ps-7.5 pe-3 py-1 text-md text-n600">{t("noChats")}</div>
              )}
            </ProjectRow>
          </div>
        )
      })}

      <Dialog open={confirmProject !== null} onClose={() => setConfirmProject(null)}>
        <DialogTitle>{t("delTitle", { name: confirmProject?.name ?? "" })}</DialogTitle>
        <DialogBody>{t("delBody")}</DialogBody>
        <DialogActions>
          <button
            type="button"
            className="text-base text-n700"
            onClick={() => setConfirmProject(null)}
          >
            {t("common:action.cancel", { ns: "common" })}
          </button>
          <button
            type="button"
            className="rounded-full bg-danger px-4.5 py-2 text-base text-bg"
            onClick={onDeleteProject}
          >
            {t("common:action.delete", { ns: "common" })}
          </button>
        </DialogActions>
      </Dialog>

      <Dialog open={confirmSession !== null} onClose={() => setConfirmSession(null)}>
        <DialogTitle>{t("delChatTitle")}</DialogTitle>
        <DialogBody>{t("delChatBody")}</DialogBody>
        <DialogActions>
          <button type="button" className="text-base text-n700" onClick={() => setConfirmSession(null)}>
            {t("common:action.cancel", { ns: "common" })}
          </button>
          <button
            type="button"
            className="rounded-full bg-danger px-4.5 py-2 text-base text-bg"
            onClick={onDeleteSession}
          >
            {t("common:action.delete", { ns: "common" })}
          </button>
        </DialogActions>
      </Dialog>
    </>
  )
}
