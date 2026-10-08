import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { ChevronRight } from "lucide-react"
import { cn } from "@/shared/lib/cn"
import { formatRelative } from "@/shared/lib/format"
import { paths } from "@/shared/router/paths"
import { Dialog, DialogActions, DialogBody, DialogTitle } from "@/shared/ui/Dialog"
import { useDeleteCronJob, useRunCronJob, useUpdateCronJob } from "@/features/cron/api/cron"
import { describeSchedule } from "@/features/cron/utils/schedule"
import { isAutoDisabled, jobDotClass } from "@/features/cron/utils/jobs"
import type { CronJob } from "@/features/cron/types"

const actionCls = "min-h-8 rounded-full border border-hair px-3 text-xs text-n800 hover:bg-hairsoft"

export function StateDot({ job }: { job: CronJob }) {
  const { t } = useTranslation("cron")
  const label = job.running
    ? t("job.state.running")
    : job.enabled
      ? t("job.state.enabled")
      : isAutoDisabled(job)
        ? t("job.state.autoDisabled")
        : t("job.state.disabled")
  return (
    <span className="text-n600 flex items-center gap-1.5 text-xs">
      <span className={cn("size-1.5 rounded-full", jobDotClass(job))} />
      {label}
    </span>
  )
}

/** One job on the list page; its runs and transcripts live on the task page. */
export function CronJobCard({ job, onEdit }: { job: CronJob; onEdit: (job: CronJob) => void }) {
  const { t } = useTranslation("cron")
  const [confirming, setConfirming] = useState(false)
  const update = useUpdateCronJob()
  const remove = useDeleteCronJob()
  const runNow = useRunCronJob()

  const busy = update.isPending || remove.isPending || runNow.isPending

  if (job.management === "assistant") {
    return (
      <div className="border-hair bg-card flex flex-col gap-2 rounded-lg border px-4 py-3.5">
        <span className="text-ink">{job.name}</span>
        <span className="text-n600 text-xs">{describeSchedule(job.schedule, t)}</span>
        <Link to={paths.assistant} className="text-ink text-sm underline">{t("job.manageInAssistant")}</Link>
      </div>
    )
  }

  return (
    <div className="border-hair bg-card flex flex-col rounded-lg border px-4 py-3.5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <Link to={paths.cronJob(job.id)} className="text-ink text-base underline-offset-2 hover:underline">
          {job.name}
        </Link>
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
          <button type="button" className={actionCls} disabled={busy} onClick={() => onEdit(job)}>
            {t("job.action.edit")}
          </button>
          <button
            type="button"
            className={cn(actionCls, "text-danger")}
            disabled={busy}
            onClick={() => setConfirming(true)}
          >
            {t("job.action.delete")}
          </button>
        </span>
      </div>

      <span className="text-n700 mt-1 line-clamp-2 text-sm text-pretty">{job.task_prompt}</span>

      <div className="text-n600 mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        <span>{describeSchedule(job.schedule, t)}</span>
        {job.enabled && job.next_run_at && (
          <span>{t("job.nextRun", { when: formatRelative(job.next_run_at) })}</span>
        )}
        {job.last_run_at && <span>{t("job.lastRun", { when: formatRelative(job.last_run_at) })}</span>}
        <span>
          {t("job.stats", { total: job.total_runs, ok: job.total_successes, failed: job.total_failures })}
        </span>
        {job.project_directory && <span className="truncate">{job.project_directory}</span>}
        <Link
          to={paths.cronJob(job.id)}
          className="text-ink flex items-center gap-0.5 underline-offset-2 hover:underline"
        >
          {t("job.showRuns")}
          <ChevronRight size={12} strokeWidth={2.4} />
        </Link>
      </div>

      {(job.last_error ?? "") !== "" && !job.enabled && (
        <span className="text-danger mt-1.5 line-clamp-2 text-xs text-pretty">{t("job.lastError")}</span>
      )}

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
            onClick={() => remove.mutate(job.id, { onSettled: () => setConfirming(false) })}
          >
            {t("job.action.delete")}
          </button>
        </DialogActions>
      </Dialog>
    </div>
  )
}
