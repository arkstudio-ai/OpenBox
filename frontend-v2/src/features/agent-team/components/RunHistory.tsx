import { useState } from "react"
import { Link } from "react-router"
import { useTranslation } from "react-i18next"
import type { Project } from "@/shared/types/api"
import { paths } from "@/shared/router/paths"
import { Spinner } from "@/shared/ui/Spinner"
import { Dialog, DialogActions, DialogTitle } from "@/shared/ui/Dialog"
import { useDefinitions, useTeamRuns } from "../api/teams"
import { terminalTeam, type TeamRunInfo, type TeamSpec } from "../types"
import { BUTTON, INPUT } from "./FormFields"
import { UsageSummary } from "./TeamUsagePanel"

const STATUSES = ["running", "paused", "completed", "canceled", "failed"]

export function RunHistory({
  projects,
  onDelete,
}: {
  projects: Project[]
  onDelete: (id: string) => Promise<unknown>
}) {
  const { t } = useTranslation("teams")
  const [status, setStatus] = useState("")
  const [project, setProject] = useState("")
  const [template, setTemplate] = useState("")
  const [deleting, setDeleting] = useState<TeamRunInfo | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const templates = useDefinitions<TeamSpec>("team")
  const runs = useTeamRuns({
    status: status || undefined,
    project_id: project || undefined,
    template_id: template || undefined,
  })
  const rows = runs.data?.pages.flatMap((page) => page.items) ?? []
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        <select
          aria-label={t("filterStatus")}
          value={status}
          onChange={(event) => setStatus(event.target.value)}
          className={`${INPUT} w-auto`}
        >
          <option value="">{t("allStates")}</option>
          {STATUSES.map((value) => (
            <option key={value} value={value}>
              {t(`state.${value}`)}
            </option>
          ))}
        </select>
        <select
          aria-label={t("project")}
          value={project}
          onChange={(event) => setProject(event.target.value)}
          className={`${INPUT} w-auto`}
        >
          <option value="">{t("allProjects")}</option>
          {projects.map((value) => (
            <option key={value.id} value={value.id}>
              {value.name}
            </option>
          ))}
        </select>
        <select
          aria-label={t("template")}
          value={template}
          onChange={(event) => setTemplate(event.target.value)}
          className={`${INPUT} w-auto`}
        >
          <option value="">{t("allTemplates")}</option>
          {templates.data?.pages
            .flatMap((page) => page.items)
            .map((value) => (
              <option key={value.id} value={value.id}>
                {value.name}
              </option>
            ))}
        </select>
      </div>
      {runs.isLoading && <Spinner className="size-5" />}
      {runs.error && <p className="text-danger text-sm">{runs.error.message}</p>}
      {!runs.isLoading && !runs.error && rows.length === 0 && (
        <p className="text-n600 border-hair bg-card rounded-xl border p-5 text-sm">{t("emptyRuns")}</p>
      )}
      {rows.map((run) => (
        <article key={run.id} className="bg-hairsoft block rounded-xl p-4">
          <div className="flex items-center justify-between gap-3">
            <Link
              to={paths.teamChat(run.root_session_id)}
              className="truncate text-sm font-medium hover:underline"
            >
              {run.title}
            </Link>
            <span className="text-n600 text-xs">{t(`state.${run.state}`)}</span>
          </div>
          {run.summary?.needs_attention && <p className="text-a700 mt-2 text-xs">{t("needsAttention")}</p>}
          <p className="text-n600 mt-2 text-xs">
            {projects.find((item) => item.id === run.project_id)?.name ?? t("project")} ·{" "}
            {new Date(run.created_at).toLocaleString()}
          </p>
          {run.summary?.pause_reason && (
            <p className="text-a700 mt-2 text-xs">
              {t(`reason.${run.summary.pause_reason}`, { defaultValue: run.summary.pause_reason })}
            </p>
          )}
          {run.summary?.task_count != null && (
            <p className="text-n600 mt-1 text-xs">{t("totalTasks", { count: run.summary.task_count })}</p>
          )}
          {run.usage && (
            <div className="mt-2">
              <UsageSummary usage={run.usage} />
            </div>
          )}
          <div className="mt-3 flex flex-wrap gap-3">
            <Link to={paths.teamChat(run.root_session_id)} className="text-a700 py-1.5 text-xs">
              {t("openChat")}
            </Link>
            <Link to={paths.rerunTeam(run.id, run.project_id)} className="text-a700 py-1.5 text-xs">
              {t("runAgain")}
            </Link>
            <button
              type="button"
              disabled={!terminalTeam(run.state)}
              className="text-n600 py-1.5 text-xs disabled:opacity-40"
              onClick={() => {
                setError("")
                setDeleting(run)
              }}
            >
              {t("deleteRun")}
            </button>
          </div>
        </article>
      ))}
      {runs.hasNextPage && (
        <button type="button" onClick={() => void runs.fetchNextPage()} className={BUTTON}>
          {t("loadMore")}
        </button>
      )}
      <Dialog
        open={!!deleting}
        onClose={() => {
          if (!busy) setDeleting(null)
        }}
        label={t("deleteRun")}
      >
        <DialogTitle>{t("deleteRun")}</DialogTitle>
        <p className="text-n600 text-sm">{t("deleteRunHint", { title: deleting?.title })}</p>
        {error && (
          <p role="alert" className="text-danger text-sm">
            {error}
          </p>
        )}
        <DialogActions>
          <button type="button" className={BUTTON} disabled={busy} onClick={() => setDeleting(null)}>
            {t("cancel")}
          </button>
          <button
            type="button"
            className={`${BUTTON} text-danger`}
            disabled={busy || !deleting}
            onClick={() => {
              if (!deleting) return
              setBusy(true)
              void onDelete(deleting.root_session_id)
                .then(() => {
                  setDeleting(null)
                  void runs.refetch()
                })
                .catch((reason: unknown) =>
                  setError(reason instanceof Error ? reason.message : String(reason)),
                )
                .finally(() => setBusy(false))
            }}
          >
            {t("deleteRun")}
          </button>
        </DialogActions>
      </Dialog>
    </div>
  )
}
