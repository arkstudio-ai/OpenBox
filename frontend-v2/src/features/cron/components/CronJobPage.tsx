// One scheduled task: its settings and actions up top, the run history down
// the side, and what the picked run did beside it. The transcript is a
// conversation the chat feature renders; the route hands it in, because
// features never import each other (ENGINEERING_SPEC §4).
import { useState, type ReactNode } from "react"
import { useTranslation } from "react-i18next"
import { Link, useNavigate, useSearchParams } from "react-router"
import { ArrowLeft } from "lucide-react"
import { cn } from "@/shared/lib/cn"
import { formatRelative } from "@/shared/lib/format"
import { paths } from "@/shared/router/paths"
import { Dialog, DialogActions, DialogBody, DialogTitle } from "@/shared/ui/Dialog"
import { Spinner } from "@/shared/ui/Spinner"
import { useCronJobs, useDeleteCronJob, useRunCronJob, useUpdateCronJob } from "@/features/cron/api/cron"
import { CronJobForm } from "@/features/cron/components/CronJobForm"
import { StateDot } from "@/features/cron/components/CronJobCard"
import { CronRunList } from "@/features/cron/components/CronRunList"
import { useCronRuns } from "@/features/cron/api/cron"
import { describeSchedule } from "@/features/cron/utils/schedule"
import { pickRun } from "@/features/cron/utils/jobs"
import type { CronJob, CronRun } from "@/features/cron/types"

const actionCls = "min-h-8 rounded-full border border-hair px-3 text-xs text-n800 hover:bg-hairsoft"
/** Query param naming the run in view, so a reload or a shared link keeps it. */
const RUN_PARAM = "run"

interface Props {
  jobId: string
  renderTranscript: (sessionId: string) => ReactNode
}

function BackLink() {
  const { t } = useTranslation("cron")
  return (
    <Link
      to={paths.cron}
      className="text-n600 hover:text-ink flex items-center gap-1 self-start text-sm underline-offset-2 hover:underline"
    >
      <ArrowLeft size={14} strokeWidth={2.4} aria-hidden />
      {t("detail.back")}
    </Link>
  )
}

function JobHeader({ job, onEdit, onDelete }: { job: CronJob; onEdit: () => void; onDelete: () => void }) {
  const { t } = useTranslation("cron")
  const update = useUpdateCronJob()
  const runNow = useRunCronJob()
  const busy = update.isPending || runNow.isPending
  return (
    <header className="border-hair bg-card flex flex-col gap-2 rounded-2xl border px-4.5 py-4">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <h1 className="text-ink text-xl font-medium tracking-tight">{job.name}</h1>
        <StateDot job={job} />
        <span className="ms-auto flex items-center gap-1.5">
          <button
            type="button"
            className={actionCls}
            disabled={busy || job.running}
            onClick={() => runNow.mutate(job.id)}
          >
            {t("job.action.runNow")}
          </button>
          <button
            type="button"
            className={actionCls}
            disabled={busy}
            onClick={() => update.mutate({ jobId: job.id, patch: { enabled: !job.enabled } })}
          >
            {job.enabled ? t("job.action.disable") : t("job.action.enable")}
          </button>
          <button type="button" className={actionCls} disabled={busy} onClick={onEdit}>
            {t("job.action.edit")}
          </button>
          <button type="button" className={cn(actionCls, "text-danger")} disabled={busy} onClick={onDelete}>
            {t("job.action.delete")}
          </button>
        </span>
      </div>
      <p className="text-n700 line-clamp-3 text-sm text-pretty" title={job.task_prompt}>
        {job.task_prompt}
      </p>
      <div className="text-n600 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        <span>{describeSchedule(job.schedule, t)}</span>
        {job.enabled && job.next_run_at && (
          <span>{t("job.nextRun", { when: formatRelative(job.next_run_at) })}</span>
        )}
        {job.last_run_at && <span>{t("job.lastRun", { when: formatRelative(job.last_run_at) })}</span>}
        <span>
          {t("job.stats", { total: job.total_runs, ok: job.total_successes, failed: job.total_failures })}
        </span>
        {job.project_directory && <span className="truncate">{job.project_directory}</span>}
      </div>
      {(job.last_error ?? "") !== "" && !job.enabled && (
        <span className="text-danger line-clamp-2 text-xs text-pretty">{t("job.lastError")}</span>
      )}
    </header>
  )
}

function Empty({ text }: { text: string }) {
  return <p className="text-n600 flex flex-1 items-center justify-center p-6 text-center text-sm">{text}</p>
}

/** What the picked run did: its transcript when it left one, with the failure
 *  reason pinned above it when the run ended badly. */
function RunDetail({
  run,
  renderTranscript,
}: {
  run: CronRun | null
  renderTranscript: Props["renderTranscript"]
}) {
  const { t } = useTranslation("cron")
  if (!run) return <Empty text={t("detail.pickRun")} />
  return (
    <>
      {run.status === "error" && (
        <div className="border-hair flex flex-none flex-wrap items-baseline gap-x-2 border-b px-4 py-2 text-xs">
          <span className="text-danger">{t("run.failed")}</span>
          {run.error_message && <span className="text-n700 break-all">{run.error_message}</span>}
        </div>
      )}
      {run.temp_session_id ? (
        renderTranscript(run.temp_session_id)
      ) : (
        <Empty text={t("detail.noTranscript")} />
      )}
    </>
  )
}

export function CronJobPage({ jobId, renderTranscript }: Props) {
  const { t } = useTranslation("cron")
  const navigate = useNavigate()
  const jobs = useCronJobs()
  const job = (jobs.data ?? []).find((j) => j.id === jobId) ?? null
  const runs = useCronRuns(jobId, job !== null, job?.running ?? false)
  const remove = useDeleteCronJob()
  const [params, setParams] = useSearchParams()
  const [editing, setEditing] = useState(false)
  const [confirming, setConfirming] = useState(false)

  const selected = pickRun(runs.data ?? [], params.get(RUN_PARAM))
  const select = (run: CronRun) => {
    const next = new URLSearchParams(params)
    next.set(RUN_PARAM, run.id)
    setParams(next, { replace: true })
  }

  if (jobs.isPending) {
    return (
      <div className="flex flex-1 items-center justify-center py-10">
        <Spinner className="size-5" />
      </div>
    )
  }
  if (!job) {
    return (
      <div className="flex flex-col gap-3">
        <BackLink />
        <div className="border-hair bg-card text-n600 rounded-lg border px-4 py-6 text-sm">
          {jobs.isError ? t("page.loadFailed") : t("detail.notFound")}
        </div>
      </div>
    )
  }

  if (job.management === "assistant") {
    return (
      <div className="flex flex-col gap-3">
        <BackLink />
        <h1 className="text-ink text-xl">{job.name}</h1>
        <Link to={paths.assistant} className="text-ink underline">{t("job.manageInAssistant")}</Link>
      </div>
    )
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      <BackLink />
      <JobHeader job={job} onEdit={() => setEditing(true)} onDelete={() => setConfirming(true)} />

      <div className="flex min-h-0 flex-1 flex-col gap-3 md:flex-row">
        <aside className="flex max-h-64 flex-none flex-col md:max-h-none md:w-72">
          <span className="text-n600 px-3 pb-1 text-xs font-medium">{t("detail.runs")}</span>
          <div className="scr min-h-0 flex-1 overflow-auto">
            <CronRunList
              jobId={jobId}
              selectedRunId={selected?.id ?? null}
              onSelect={select}
              live={job.running}
            />
          </div>
        </aside>
        <section className="border-hair flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-2xl border">
          <RunDetail run={selected} renderTranscript={renderTranscript} />
        </section>
      </div>

      <CronJobForm open={editing} onClose={() => setEditing(false)} job={editing ? job : null} />

      <Dialog open={confirming} onClose={() => setConfirming(false)}>
        <DialogTitle>{t("job.deleteConfirm.title")}</DialogTitle>
        <DialogBody>{t("job.deleteConfirm.body", { name: job.name })}</DialogBody>
        <DialogActions>
          <button
            type="button"
            className="text-n700 min-h-9 rounded-full px-4 text-sm"
            onClick={() => setConfirming(false)}
          >
            {t("form.cancel")}
          </button>
          <button
            type="button"
            className="bg-danger text-bg min-h-9 rounded-full px-5 text-sm"
            onClick={() =>
              remove.mutate(job.id, {
                onSuccess: () => void navigate(paths.cron),
                onSettled: () => setConfirming(false),
              })
            }
          >
            {t("job.action.delete")}
          </button>
        </DialogActions>
      </Dialog>
    </div>
  )
}
