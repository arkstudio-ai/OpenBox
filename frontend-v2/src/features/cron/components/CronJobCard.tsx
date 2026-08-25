import { useState } from "react"
import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"
import { formatRelative } from "@/shared/lib/format"
import { Dialog, DialogActions, DialogBody, DialogTitle } from "@/shared/ui/Dialog"
import { useDeleteCronJob, useRunCronJob, useUpdateCronJob } from "@/features/cron/api/cron"
import { describeSchedule } from "@/features/cron/utils/schedule"
import { CronRunList } from "@/features/cron/components/CronRunList"
import type { CronJob } from "@/features/cron/types"

const actionCls = "min-h-8 rounded-full border border-hair px-3 text-xs text-n800 hover:bg-hairsoft"

function StateDot({ job }: { job: CronJob }) {
  const { t } = useTranslation("cron")
  const auto = (job.last_error ?? "").startsWith("[auto-disabled")
  const label = job.running
    ? t("job.state.running")
    : job.enabled
      ? t("job.state.enabled")
      : auto
        ? t("job.state.autoDisabled")
        : t("job.state.disabled")
  return (
    <span className="flex items-center gap-1.5 text-xs text-n600">
      <span
        className={cn(
          "size-1.5 rounded-full",
          job.running ? "bg-accent" : job.enabled ? "bg-sage" : auto ? "bg-danger" : "bg-n400",
        )}
      />
      {label}
    </span>
  )
}

export function CronJobCard({ job, onEdit }: { job: CronJob; onEdit: (job: CronJob) => void }) {
  const { t } = useTranslation("cron")
  const [expanded, setExpanded] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const update = useUpdateCronJob()
  const remove = useDeleteCronJob()
  const runNow = useRunCronJob()

  const busy = update.isPending || remove.isPending || runNow.isPending

  return (
    <div className="flex flex-col rounded-lg border border-hair bg-card px-4 py-3.5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <span className="text-base text-ink">{job.name}</span>
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

      <span className="mt-1 line-clamp-2 text-pretty text-sm text-n700">{job.task_prompt}</span>

      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-n600">
        <span>{describeSchedule(job.schedule, t)}</span>
        {job.enabled && job.next_run_at && (
          <span>{t("job.nextRun", { when: formatRelative(job.next_run_at) })}</span>
        )}
        {job.last_run_at && <span>{t("job.lastRun", { when: formatRelative(job.last_run_at) })}</span>}
        <span>
          {t("job.stats", { total: job.total_runs, ok: job.total_successes, failed: job.total_failures })}
        </span>
        {job.project_directory && <span className="truncate">{job.project_directory}</span>}
        <button
          type="button"
          className="text-ink underline-offset-2 hover:underline"
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? t("job.hideRuns") : t("job.showRuns")}
        </button>
      </div>

      {(job.last_error ?? "") !== "" && !job.enabled && (
        <span className="mt-1.5 line-clamp-2 text-pretty text-xs text-danger">{t("job.lastError")}</span>
      )}

      <div className={cn("mt-2", !expanded && "hidden")}>
        <CronRunList jobId={job.id} open={expanded} />
      </div>

      <Dialog open={confirming} onClose={() => setConfirming(false)}>
        <DialogTitle>{t("job.deleteConfirm.title")}</DialogTitle>
        <DialogBody>{t("job.deleteConfirm.body", { name: job.name })}</DialogBody>
        <DialogActions>
          <button
            type="button"
            className="min-h-9 rounded-full px-4 text-sm text-n700"
            onClick={() => setConfirming(false)}
          >
            {t("form.cancel")}
          </button>
          <button
            type="button"
            className="min-h-9 rounded-full bg-danger px-5 text-sm text-bg"
            onClick={() => remove.mutate(job.id, { onSettled: () => setConfirming(false) })}
          >
            {t("job.action.delete")}
          </button>
        </DialogActions>
      </Dialog>
    </div>
  )
}
