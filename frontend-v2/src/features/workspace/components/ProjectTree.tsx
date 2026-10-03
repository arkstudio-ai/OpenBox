import { useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { useMatch, useNavigate } from "react-router"
import { memoryApi } from "@/shared/api/memory"
import type { Project, Session } from "@/shared/types/api"
import { Dialog, DialogActions, DialogBody, DialogTitle } from "@/shared/ui/Dialog"
import { paths, routePatterns } from "@/shared/router/paths"
import { useDeleteProject } from "../api/projects"
import { useDeleteSession } from "../api/sessions"
import { useWorkspaceUi } from "../stores/ui"
import { ProjectRow } from "./ProjectRow"
import { SessionRow } from "./SessionRow"

interface ProjectTreeProps {
  projects: Project[]
  sessions: Session[]
  searching: boolean
}

interface Group {
  project: Project | null
  sessions: Session[]
}

// Internal enum values, not copy (§10.4).
const UNSORTED = "unsorted"
const CRON_KIND = "cron"

export function ProjectTree({ projects, sessions, searching }: ProjectTreeProps) {
  const { t } = useTranslation("workspace")
  const navigate = useNavigate()
  // A trajectory detail URL also carries a `:sessionId`, but it names another
  // user's session; only the chat route marks a row of this tree as active.
  const activeSessionId = useMatch(`${paths.app}/${routePatterns.chat}`)?.params.sessionId
  const deleteProject = useDeleteProject()
  const deleteSession = useDeleteSession()
  const [confirmProject, setConfirmProject] = useState<Project | null>(null)
  const [confirmSession, setConfirmSession] = useState<Session | null>(null)

  const groups = useMemo<Group[]>(() => {
    const byProject = new Map<string, Session[]>()
    const loose: Session[] = []
    for (const s of sessions) {
      // A scheduled run's transcript belongs to its task: it is read from the
      // task's page, and listed here it passed for a conversation someone had.
      if (s.kind === CRON_KIND || s.kind === "assistant") continue
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
    if (
      groups.some(
        (g) => g.project?.id === confirmProject.id && g.sessions.some((s) => s.id === activeSessionId),
      )
    ) {
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
        const groupId = g.project?.id ?? UNSORTED
        return (
          <div key={groupId} className="flex flex-col">
            <ProjectRow
              project={g.project}
              forceExpanded={searching}
              onAskDelete={() => g.project && setConfirmProject(g.project)}
            >
              {g.sessions.map((s) => (
                <SessionRow
                  key={s.id}
                  session={s}
                  active={s.id === activeSessionId}
                  onAskDelete={() => setConfirmSession(s)}
                />
              ))}
              {g.sessions.length === 0 && (
                <div className="text-md text-n600 py-1 ps-7.5 pe-3">{t("noChats")}</div>
              )}
            </ProjectRow>
          </div>
        )
      })}

      <Dialog open={confirmProject !== null} onClose={() => setConfirmProject(null)}>
        <DialogTitle>{t("delTitle", { name: confirmProject?.name ?? "" })}</DialogTitle>
        <DialogBody>{t("delBody")}</DialogBody>
        <DialogActions>
          <button type="button" className="text-n700 text-base" onClick={() => setConfirmProject(null)}>
            {t("common:action.cancel", { ns: "common" })}
          </button>
          <button
            type="button"
            className="bg-danger text-bg rounded-full px-4.5 py-2 text-base"
            onClick={onDeleteProject}
          >
            {t("common:action.delete", { ns: "common" })}
          </button>
        </DialogActions>
      </Dialog>

      <Dialog open={confirmSession !== null} onClose={() => setConfirmSession(null)}>
        <DialogTitle>{t("delChatTitle")}</DialogTitle>
        <DialogBody>{t("delChatBody")}</DialogBody>
        {confirmSession && <LearnedFromChat sessionId={confirmSession.id} />}
        <DialogActions>
          <button type="button" className="text-n700 text-base" onClick={() => setConfirmSession(null)}>
            {t("common:action.cancel", { ns: "common" })}
          </button>
          <button
            type="button"
            className="bg-danger text-bg rounded-full px-4.5 py-2 text-base"
            onClick={onDeleteSession}
          >
            {t("common:action.delete", { ns: "common" })}
          </button>
        </DialogActions>
      </Dialog>
    </>
  )
}

/** Deleting a chat also retires what the assistant learned from it; say so first. */
function LearnedFromChat({ sessionId }: { sessionId: string }) {
  const { t } = useTranslation("workspace")
  const learned = useQuery({
    queryKey: ["memory-learned-from", sessionId],
    queryFn: () => memoryApi.learnedFrom(sessionId),
    retry: false,
    staleTime: 0,
  })
  const count = learned.data?.count ?? 0
  return count > 0 ? <DialogBody>{t("delChatMemories", { count })}</DialogBody> : null
}
