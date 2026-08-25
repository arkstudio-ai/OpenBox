import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { Spinner } from "@/shared/ui/Spinner"
import { cn } from "@/shared/lib/cn"
import { paths } from "@/shared/router/paths"
import { formatRelative } from "@/shared/lib/format"
import { useCronJobs, useRunCronJob, useUpdateCronJob } from "@/features/cron/api/cron"
import { useCurrentProjectId } from "@/features/cron/hooks/useCurrentProjectId"
import { describeSchedule } from "@/features/cron/utils/schedule"
import { CronRunList } from "@/features/cron/components/CronRunList"
import type { CronJob } from "@/features/cron/types"

const actionCls = "min-h-7 rounded-full border border-hair px-2.5 text-xs text-n800 hover:bg-hairsoft"

/** Compact per-session job row for the workbench panel. */
function PanelJobRow({ job }: { job: CronJob }) {
  const { t } = useTranslation("cron")
  const [expanded, setExpanded] = useState(false)
  const update = useUpdateCronJob()
  const runNow = useRunCronJob()
  const busy = update.isPending || runNow.isPending

  const dotCls = job.running
    ? "bg-accent animate-pulse"
    : job.enabled
      ? "bg-sage"
      : (job.last_error ?? "").startsWith("[auto-disabled")
        ? "bg-danger"
        : "bg-n400"

  return (
    <div className="flex flex-col rounded-lg border border-hair bg-card px-3.5 py-3">
      <div className="flex items-center gap-2">
        <span className={cn("size-1.5 flex-none rounded-full", dotCls)} />
        <span className="min-w-0 flex-1 truncate text-sm text-ink">{job.name}</span>
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
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-x-2.5 gap-y-1 text-xs text-n600">
        <span>{describeSchedule(job.schedule, t)}</span>
        {job.enabled && job.next_run_at && (
          <span>{t("job.nextRun", { when: formatRelative(job.next_run_at) })}</span>
        )}
        {job.last_run_at && <span>{t("job.lastRun", { when: formatRelative(job.last_run_at) })}</span>}
        <button
          type="button"
          className="text-ink underline-offset-2 hover:underline"
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? t("job.hideRuns") : t("job.showRuns")}
        </button>
      </div>
      <div className={cn("mt-1.5", !expanded && "hidden")}>
        <CronRunList jobId={job.id} open={expanded} />
      </div>
    </div>
  )
}

/** The workbench "scheduled tasks" tab: the current project's jobs. */
export function CronPanelTab({ sessionId }: { sessionId: string | null }) {
  const { t } = useTranslation("cron")
  const jobs = useCronJobs()
  const projectId = useCurrentProjectId(sessionId)

  const mine = (jobs.data ?? []).filter((j) => j.project_id != null && j.project_id === projectId)

  return (
    <div className="scr flex min-h-0 flex-1 flex-col gap-2.5 overflow-auto px-3.5 pt-1.5 pb-4">
      {jobs.isPending && (
        <div className="flex items-center gap-2 py-4 text-sm text-n600">
          <Spinner />
          <span>{t("page.loading")}</span>
        </div>
      )}

      {jobs.isError && <span className="py-4 text-sm text-danger">{t("page.loadFailed")}</span>}

      {jobs.isSuccess && (!projectId || mine.length === 0) && (
        <div className="flex flex-col gap-1 rounded-lg border border-hair bg-card px-3.5 py-5">
          <span className="text-sm text-ink">{t("panel.empty.title")}</span>
          <span className="text-pretty text-xs text-n600">{t("panel.empty.body")}</span>
        </div>
      )}

      {mine.map((job) => (
        <PanelJobRow key={job.id} job={job} />
      ))}

      <Link
        to={paths.cron}
        className="mt-1 self-start text-xs text-n600 underline-offset-2 hover:text-ink hover:underline"
      >
        {t("panel.manageAll")}
      </Link>
    </div>
  )
}
