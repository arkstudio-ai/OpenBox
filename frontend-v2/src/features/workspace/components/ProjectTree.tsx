import { useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { useMatch, useNavigate } from "react-router"
import { memoryApi } from "@/shared/api/memory"
import type { Project, Session } from "@/shared/types/api"
import { Dialog, DialogActions, DialogBody, DialogTitle } from "@/shared/ui/Dialog"
import { paths, routePatterns } from "@/shared/router/paths"
import { useDeleteProject } from "../api/projects"
import { useBatchDeleteSessions, useDeleteSession, useRenameSession } from "../api/sessions"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceUi } from "../stores/ui"
import { ProjectRow } from "./ProjectRow"
import { ProjectBriefDialog } from "./ProjectBriefDialog"
import { SessionRow } from "./SessionRow"

interface ProjectTreeProps {
  projects: Project[]
  sessions: Session[]
}

interface Group {
  project: Project | null
  sessions: Session[]
}

// Internal enum values, not copy (§10.4).
const UNSORTED = "unsorted"
const CRON_KIND = "cron"

export function ProjectTree({ projects, sessions }: ProjectTreeProps) {
  const { t } = useTranslation("workspace")
  const navigate = useNavigate()
  // A trajectory detail URL also carries a `:sessionId`, but it names another
  // user's session; only the chat route marks a row of this tree as active.
  const activeSessionId = useMatch(`${paths.app}/${routePatterns.chat}`)?.params.sessionId
  const deleteProject = useDeleteProject()
  const deleteSession = useDeleteSession()
  const renameSession = useRenameSession()
  const batchDelete = useBatchDeleteSessions()
  const userId = useAuthStore((state) => state.user?.id)
  const own = (session: Session) => !session.user_id || session.user_id === userId
  const [rename, setRename] = useState<{ id: string; title: string } | null>(null)
  const [selecting, setSelecting] = useState(false)
  const [selected, setSelected] = useState(new Set<string>())
  const [batchConfirm, setBatchConfirm] = useState(false)
  const [batchResult, setBatchResult] = useState<{ deleted: number; failed: number } | null>(null)

  const [confirmProject, setConfirmProject] = useState<Project | null>(null)
  const [briefProject, setBriefProject] = useState<Project | null>(null)
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

  const beginRename = (session: Session) => {
    renameSession.reset()
    setRename({ id: session.id, title: session.title ?? "" })
  }

  const submitRename = (event: React.FormEvent) => {
    event.preventDefault()
    if (!rename?.title.trim()) return
    renameSession.mutate({ ...rename, title: rename.title.trim() }, { onSuccess: () => setRename(null) })
  }

  const submitBatch = () => {
    batchDelete.mutate([...selected], {
      onSuccess: (result) => {
        setBatchConfirm(false)
        setSelected(new Set(result.failed))
        setSelecting(result.failed.length > 0)
        setBatchResult({ deleted: result.deleted.length, failed: result.failed.length })
        if (activeSessionId && result.deleted.includes(activeSessionId)) navigate(paths.app)
      },
    })
  }

  return (
    <>
      <div className="flex items-center justify-between gap-2 px-1.5 py-1">
        {selecting ? (
          <>
            <span className="text-xs text-n700" aria-live="polite">{t("chatSelection", { count: selected.size })}</span>
            <button type="button" disabled={batchDelete.isPending} className="text-xs text-n700" onClick={() => {
              setSelecting(false)
              setSelected(new Set())
            }}>{t("common:action.cancel", { ns: "common" })}</button>
          </>
        ) : (
          <button type="button" className="rounded-full px-2 py-1 text-xs text-n800 hover:bg-hairsoft"
            onClick={() => { setBatchResult(null); setSelecting(true) }}>
            {t("manageChats")}
          </button>
        )}
      </div>
      {batchResult && <p className="px-2 text-xs text-n700" role="status">
        {t("chatBatchResult", batchResult)}
      </p>}
      {groups.map((g) => {
        const groupId = g.project?.id ?? UNSORTED
        return (
          <div key={groupId} className="flex flex-col">
            <ProjectRow
              project={g.project}
              onAskDelete={() => g.project && setConfirmProject(g.project)}
              onOpenBrief={g.project ? () => setBriefProject(g.project) : undefined}
            >
              {g.sessions.map((s) => (
                <SessionRow
                  key={s.id}
                  session={s}
                  active={s.id === activeSessionId}
                  owned={own(s)}
                  onAskDelete={() => setConfirmSession(s)}
                  onAskRename={() => beginRename(s)}
                  selection={selecting ? {
                    checked: selected.has(s.id),
                    disabled: batchDelete.isPending || (!selected.has(s.id) && selected.size >= 100),
                    toggle: () => setSelected((previous) => {
                      const next = new Set(previous)
                      if (next.has(s.id)) next.delete(s.id)
                      else next.add(s.id)
                      return next
                    }),
                  } : undefined}
                />
              ))}
              {g.sessions.length === 0 && (
                <div className="text-md text-n600 py-1 ps-7.5 pe-3">{t("noChats")}</div>
              )}
            </ProjectRow>
          </div>
        )
      })}

      {selecting && (
        <button type="button" disabled={!selected.size || batchDelete.isPending}
          className="bg-danger text-bg sticky bottom-0 mx-1.5 rounded-full px-4 py-2 text-xs disabled:opacity-50"
          onClick={() => { batchDelete.reset(); setBatchConfirm(true) }}>
          {t("deleteChats", { count: selected.size })}
        </button>
      )}
      <Dialog open={rename !== null} onClose={() => { if (!renameSession.isPending) setRename(null) }}>
        <form onSubmit={submitRename}>
          <DialogTitle>{t("renameChat")}</DialogTitle>
          <DialogBody>
            <input aria-label={t("chatTitle")} maxLength={255} value={rename?.title ?? ""}
              className="border-hair bg-bg text-ink w-full rounded-lg border px-3 py-2"
              onChange={(event) => setRename((current) => current && { ...current, title: event.target.value })} />
            {renameSession.isError && <p role="alert" className="text-danger mt-2">{t("renameChatFailed")}</p>}
          </DialogBody>
          <DialogActions>
            <button type="button" disabled={renameSession.isPending} onClick={() => setRename(null)}>
              {t("common:action.cancel", { ns: "common" })}
            </button>
            <button type="submit" disabled={!rename?.title.trim() || renameSession.isPending}
              className="bg-a700 text-bg rounded-full px-4 py-2 disabled:opacity-50">
              {t("common:action.save", { ns: "common" })}
            </button>
          </DialogActions>
        </form>
      </Dialog>
      <Dialog open={batchConfirm} onClose={() => { if (!batchDelete.isPending) setBatchConfirm(false) }}>
        <DialogTitle>{t("deleteChatsTitle", { count: selected.size })}</DialogTitle>
        <DialogBody>{t("delChatBody")} {t("delChatBatchMemories")}</DialogBody>
        {batchDelete.isError && <DialogBody><span role="alert" className="text-danger">{t("deleteChatsFailed")}</span></DialogBody>}
        <DialogActions>
          <button type="button" disabled={batchDelete.isPending} onClick={() => setBatchConfirm(false)}>
            {t("common:action.cancel", { ns: "common" })}
          </button>
          <button type="button" disabled={!selected.size || batchDelete.isPending}
            className="bg-danger text-bg rounded-full px-4.5 py-2 disabled:opacity-50" onClick={submitBatch}>
            {t("deleteChats", { count: selected.size })}
          </button>
        </DialogActions>
      </Dialog>

      <ProjectBriefDialog project={briefProject} onClose={() => setBriefProject(null)} />

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
